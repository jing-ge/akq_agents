# P5 Web 控制台 — 设计文档

- 项目：akq-agents
- 阶段：P5（共 P1–P6 六阶段中的第五阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataHealth / 缓存）、P2（job_runs / events / daemon_state）、P3（factor_metrics / portfolio_snapshots / attribution）、P4（llm_calls / chat_sessions / LLMOrchestrator）

---

## §1 目标与边界

### 目标

把分散在 CLI / sqlite / markdown / parquet 里的状态聚合成一个**本地可访问的轻量 Web 控制台**，让单用户能在浏览器里看清"系统跑得怎样、组合长什么样、为什么这么选、跟 LLM 聊几句"：

- 实时仪表盘（数据健康、调度状态、最近事件）。
- 组合详情页（当日 / 历史；归因图、行业分布、单股因子贡献）。
- 因子有效性页（每个因子的 IC/IR 走势、status、激活/失能历史）。
- 任务历史页（job_runs 时间轴、失败下钻、retry 状态）。
- LLM 聊天页（复用 P4 的 ChatAgent / Orchestrator，浏览器 SSE 流式输出）。
- 简洁 API（FastAPI）+ 简洁 SPA（React + ECharts）+ 单端口部署。

### 在做什么（P5 范围）

- FastAPI 后端，复用 P1–P4 的服务（不重新实现业务）。
- 5 个核心 API 命名空间：`/health`、`/jobs`、`/portfolio`、`/factors`、`/chat`。
- 一个 React 单页应用（5 个页面 + 全局 layout），用 ECharts 出图。
- WebSocket / SSE：events 推送 + LLM 流式回复（仅 chat 用 SSE，其它页轮询）。
- 鉴权：**绑定 localhost**，不对外暴露；**单用户**口令（环境变量 `AKQ_WEB_TOKEN`），同源即可。
- 单端口：FastAPI 直接 mount 静态 SPA 构建产物，`http://127.0.0.1:8765` 出所有内容。
- CLI：`akq-agents web start | stop | status`。

### 不在做什么

- ❌ 多用户 / 团队协作 / 权限矩阵 — 单用户假设。
- ❌ 任何对外网开放 — 仅 127.0.0.1。
- ❌ 用户在 Web 上修改配置 / 编辑因子代码 — 全只读 + chat。
- ❌ 自动下单 / 委托 / 交易终端集成 — **永远禁止**（与 P4 一致）。
- ❌ 移动端独立 App / 响应式深度优化 — 桌面浏览器即可，移动可读但不保美观。
- ❌ 重型前端构建工具链（Next.js / SSR / Webpack 复杂配置）— Vite + 单 SPA 输出。
- ❌ 实时分钟级行情图 — 系统压根没分钟级数据。

---

## §2 架构

### 整体拓扑

```
┌────────────────────────────────────────────────────────────────┐
│  Browser (http://127.0.0.1:8765)                                 │
│  - React SPA (Vite 构建产物)                                      │
│  - 路由：/dashboard /portfolio /factors /jobs /chat              │
│  - 状态：React Query (5s 轮询) + EventSource (SSE for chat)      │
└────────────┬───────────────────────────────────────────────────┘
             │ HTTP / SSE (单端口)
             ▼
┌────────────────────────────────────────────────────────────────┐
│  FastAPI App  (uvicorn worker=1)                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  api/                                                      │  │
│  │  ├─ health.py    GET  /api/health                          │  │
│  │  ├─ jobs.py      GET  /api/jobs/runs                       │  │
│  │  │              GET  /api/jobs/events                       │  │
│  │  ├─ portfolio.py GET  /api/portfolio?date=...               │  │
│  │  │              GET  /api/portfolio/attribution?date=...    │  │
│  │  │              GET  /api/portfolio/history?limit=...       │  │
│  │  ├─ factors.py   GET  /api/factors                          │  │
│  │  │              GET  /api/factors/{name}/metrics            │  │
│  │  └─ chat.py      POST /api/chat/sessions                    │  │
│  │                 GET  /api/chat/sessions                     │  │
│  │                 POST /api/chat/sessions/{sid}/messages (SSE)│  │
│  │                 GET  /api/chat/sessions/{sid}/messages       │  │
│  ├─ deps.py        依赖注入：service container 单例                 │
│  ├─ auth.py        Bearer token (env AKQ_WEB_TOKEN) + localhost 检查 │
│  └─ static/        SPA 构建产物挂载                                  │
└────────────┬───────────────────────────────────────────────────┘
             │ 服务调用（进程内，不跨网络）
             ▼
┌────────────────────────────────────────────────────────────────┐
│  现有服务层（P1–P4 复用，零修改）                                  │
│  - DataRepository (P1)                                            │
│  - SchedulerStateStore (P2)                                        │
│  - FactorRegistry / portfolio_snapshots (P3)                       │
│  - LLMOrchestrator (P4)                                            │
└────────────────────────────────────────────────────────────────┘
```

### 进程模型

**单进程**：`akq-agents web start` 拉起一个 uvicorn worker；该进程 import 所有现有服务（与 daemon 进程**完全独立**，只共享 sqlite/parquet 数据文件）。

- **不与 daemon 共进程**：daemon 是 batch-heavy 长跑，web 是请求-响应；耦合在一个进程里抖动太大。
- **sqlite WAL 模式**：daemon 写 / web 读并发安全；P1 已开 WAL（如未开则 P5 setup 时开启）。
- **进程间通讯**：通过 sqlite 表 + parquet 文件，零 IPC。

### 鉴权与安全

```python
# api/auth.py
async def require_auth(request: Request, authorization: str = Header(None)):
    if request.client.host not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "non-local request rejected")
    token_env = os.environ.get("AKQ_WEB_TOKEN")
    if token_env is None:
        return   # 未设置时仅 localhost 限制（适合纯本地开发）
    if authorization != f"Bearer {token_env}":
        raise HTTPException(401, "invalid token")
```

设计决策：
- 默认 bind `127.0.0.1:8765`，配置层硬白名单（不允许 `0.0.0.0`，启动校验阻止）。
- 可选 `AKQ_WEB_TOKEN` 提供一层弱口令（人不在电脑前避免猫踩键盘）。
- 前端读 `localStorage` 存 token；首次进入空 token 时弹简单输入框。
- 全局拒绝任何"写业务"端点：API 全部 GET，唯一 POST 是 `/api/chat/...`（且只调 P4 read-only tools）。

### 单端口部署

```python
# main.py（启动时）
app.mount("/", StaticFiles(directory="dist", html=True), name="spa")
app.include_router(api_router, prefix="/api")
# /api/* → FastAPI; 其余 → SPA 静态文件
```

构建流程：
- `cd web/ && npm run build` → 输出到 `web/dist/`
- Python 包打包时把 `web/dist/` 作为 package data 一起带上
- `akq-agents web start` 启动时定位到该目录

### 配置示例（`config/web.yaml`，新增）

```yaml
web:
  bind_host: "127.0.0.1"
  bind_port: 8765
  cors_allow_localhost_only: true
  poll_intervals_ms:
    dashboard: 5000
    job_runs: 5000
    events: 3000
  chat:
    sse_keepalive_s: 15
    max_message_chars: 4000
  ui:
    title: "AKQ Agents Console"
    theme: "auto"            # auto | light | dark
    timezone: "Asia/Shanghai"
```

---

## §3 数据流与时序

### 流程 1：仪表盘（Dashboard）

```
GET /api/health
  → 聚合：
       - DataRepository.quality_report()  → DataHealth (P1)
       - SchedulerStateStore.get_daemon_state() → DaemonState (P2)
       - 最近 24h events 按 level 统计
       - 最近一次 batch.post_close 的 job_run 摘要
  → 返回：
     {
       "data_health": {...},
       "daemon": {"status": "running", "uptime_s": 3601, "last_heartbeat": "..."},
       "today_batch": {"status": "ok", "started_at": "...", "duration_ms": ...},
       "events_24h": {"info": 102, "warning": 3, "error": 0}
     }
```

前端 Dashboard 页：
- 4 个状态卡片 + 1 个事件 sparkline + 数据健康详情表。
- 5 秒轮询一次 `/api/health`。

### 流程 2：组合详情（Portfolio）

```
GET /api/portfolio?date=2026-06-17
  → 读 portfolio_snapshots WHERE as_of_date=? ORDER BY weight DESC
  → 加 `factor_metrics` 当时的快照（join by version）
  → 加 attribution.json
  → 返回：
     {
       "as_of_date": "...",
       "rows": [{symbol, name, weight, prev_weight, score, top_factors:[{name, contribution}]}],
       "industry_breakdown": [{industry, weight}],
       "attribution": {...},  # 直接转发 P3 的 schema
       "turnover": 0.18,
       "summary": "持仓 50 只，行业暴露 top3 ..."
     }

GET /api/portfolio/history?limit=30
  → 返回近 30 个交易日的 (date, n_holdings, turnover, top_industries)
  → 用于趋势图
```

前端 Portfolio 页：
- 左侧：日期选择器 + 行业 pie + turnover trend line。
- 中部：持仓表（可按 weight / score 排序）。
- 右侧：归因 bar（每个因子贡献）+ 选中股票的因子雷达图。

### 流程 3：因子有效性（Factors）

```
GET /api/factors
  → FactorRegistry.list_all() + 最近一次 factor_metrics (各 window)
  → [{name, version, status, last_ic, last_ir, last_evaluated, direction, lookback}]

GET /api/factors/{name}/metrics?window=60&limit=120
  → factor_metrics WHERE name=? AND window=? ORDER BY as_of_date DESC LIMIT ?
  → [{as_of_date, ic, ir, t_stat, decay_2, decay_5, decay_10, status}]
```

前端 Factors 页：
- 左侧：因子列表（status 染色）。
- 右侧选中后：IC 时间序列 + IR 走势 + 衰减曲线（lag-2/5/10）+ 状态变更时间轴。

### 流程 4：任务历史（Jobs）

```
GET /api/jobs/runs?limit=200&status=*&job_id=*
  → SchedulerStateStore.list_recent_runs(...)

GET /api/jobs/events?limit=200&since=...&level_min=info
  → SchedulerStateStore.list_events(...)
```

前端 Jobs 页：
- 上方：job_runs 时间轴 / 表格（status 染色：ok 绿 / failed 红 / crashed 黑）。
- 下方：events 实时流（3 秒轮询）。
- 点击任一 run → 弹出 payload_json + 关联 events。

### 流程 5：LLM 聊天（Chat）

```
POST /api/chat/sessions {label?}
  → LLMStore.new_session() → 返回 session_id

POST /api/chat/sessions/{sid}/messages
  Body: {content, model?}
  Content-Type: text/event-stream
  → LLMOrchestrator.run(stream=True)
  → 把 LLM 流式 token / ToolUse 事件以 SSE 推给前端：
       event: token  data: {"delta": "..."}
       event: tool   data: {"name": "...", "args": {...}, "result": {...}}
       event: done   data: {"message_id": ..., "tokens": ...}

GET /api/chat/sessions/{sid}/messages?limit=50
  → 历史消息（含 tool 步骤），用于打开页面或恢复会话
```

前端 Chat 页：
- 左侧：session 列表（新建 / 切换 / 关闭）。
- 中部：消息流（user / assistant / tool 卡片），SSE 增量渲染。
- 顶部：模型选择器（仅展示 P4 网关 `/healthz` 返回的 models）。
- 右侧：本次会话已调用工具列表（可点击查看 args/result，便于调试）。

**SSE 实现要点**：
- 后端循环 `async for chunk in orchestrator.stream(...)`：把 P4 的同步 `run()` 用 generator 包一层；如 P4 暂未实现 stream，则降级到"等完整响应再一次性推"。
- 心跳：每 15 秒 send `: keepalive` 注释行。
- 客户端断线后，session 不丢；下次 GET 历史可继续。

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── web/                              ← 新增子包
│   ├── __init__.py
│   ├── server.py                     # uvicorn 启动入口（不直接 import FastAPI app）
│   ├── app.py                        # FastAPI 实例 + mount static
│   ├── deps.py                       # service container（一次性构造 P1-P4 服务单例）
│   ├── auth.py                       # require_auth dependency
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── jobs.py
│   │   ├── portfolio.py
│   │   ├── factors.py
│   │   └── chat.py
│   ├── schemas/                      # FastAPI 响应 schema（pydantic v2）
│   │   ├── health.py
│   │   ├── portfolio.py
│   │   ├── factors.py
│   │   ├── jobs.py
│   │   └── chat.py
│   └── static/                       # SPA 构建产物（gitignore，构建期填入）
├── cli/
│   └── app.py                        # 新增子命令：web start/status/stop

web/                                  ← 项目根级新增前端代码（不在 python 包内）
├── package.json
├── vite.config.ts
├── index.html
├── tsconfig.json
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── routes.tsx
    ├── api/                          # axios client + types
    ├── pages/
    │   ├── Dashboard.tsx
    │   ├── Portfolio.tsx
    │   ├── Factors.tsx
    │   ├── Jobs.tsx
    │   └── Chat.tsx
    ├── components/
    │   ├── StatusCard.tsx
    │   ├── EventStream.tsx
    │   ├── IndustryPie.tsx           # ECharts
    │   ├── ICLineChart.tsx           # ECharts
    │   ├── AttributionBar.tsx        # ECharts
    │   ├── JobTimeline.tsx           # ECharts
    │   └── ChatStream.tsx
    └── lib/
        ├── sse.ts                    # SSE client
        ├── query.ts                  # react-query setup
        └── auth.ts                   # token storage
```

### 关键 API 响应 Schema（pydantic v2）

```python
# schemas/health.py
class HealthResponse(BaseModel):
    data_health: DataHealth         # 复用 P1 schema
    daemon: DaemonStatus            # 复用 P2 schema
    today_batch: JobRunSummary | None
    events_24h: EventsCountByLevel

# schemas/portfolio.py
class PortfolioResponse(BaseModel):
    as_of_date: date
    rows: list[PortfolioRow]
    industry_breakdown: list[IndustryWeight]
    attribution: dict           # P3 的 attribution.json 原样转发
    turnover: float
    summary: str                # P3 已生成的人话摘要

# schemas/chat.py
class ChatStreamEvent(BaseModel):
    type: Literal["token", "tool", "done", "error"]
    data: dict
```

### Service Container 依赖注入

```python
# web/deps.py（启动时构造一次，复用）
@lru_cache(maxsize=1)
def get_services() -> ServiceContainer:
    config = load_all_configs()
    return ServiceContainer(
        repo=DataRepository(...),                  # P1
        sched_store=SchedulerStateStore(...),      # P2
        factor_registry=FactorRegistry(...),       # P3
        portfolio_store=PortfolioStore(...),       # P3
        llm_orchestrator=LLMOrchestrator(...),     # P4
    )

# 每个 endpoint 用 Depends(get_services) 注入
@router.get("/api/health")
async def health(svc: ServiceContainer = Depends(get_services), _=Depends(require_auth)):
    return HealthResponse(...)
```

### 前端关键依赖

```json
{
  "dependencies": {
    "react": "^18",
    "react-dom": "^18",
    "react-router-dom": "^6",
    "@tanstack/react-query": "^5",
    "echarts": "^5",
    "echarts-for-react": "^3",
    "axios": "^1",
    "tailwindcss": "^3"
  },
  "devDependencies": {
    "vite": "^5",
    "typescript": "^5",
    "@vitejs/plugin-react": "^4"
  }
}
```

设计决策：
- **Tailwind**：少写 CSS，集中样式；不引入 antd/MUI（太重）。
- **ECharts**：图丰富、中文文档好、与 echarts-for-react 集成简单。
- **React Query**：处理轮询 + 缓存 + loading 状态。
- **不用 Redux/Zustand**：状态全是服务端来的，React Query 够用。

### 关键边界

- ❌ 不实现 SSR / 服务端渲染。
- ❌ 不引入 GraphQL / tRPC。
- ❌ 不做用户管理 / 注册。
- ❌ web 进程不写业务表（除 `chat_messages` 这一处，且仅在用户主动聊天时写）。
- ❌ 不嵌入富文本编辑器 / Notebook。
- ❌ 不实现实时 K 线图（无分钟级数据）。

### 测试策略

```
tests/web/
├── test_auth.py                # localhost 限制 + token 校验
├── test_api_health.py          # mock services → 检验 schema
├── test_api_portfolio.py
├── test_api_factors.py
├── test_api_jobs.py
├── test_api_chat.py            # SSE 流（用 httpx + asyncio）
└── conftest.py                 # FastAPI TestClient + 假 ServiceContainer

web/src/__tests__/              # 前端单测（vitest + RTL）
├── components/StatusCard.test.tsx
├── pages/Dashboard.test.tsx
└── lib/sse.test.ts
```

目标覆盖率：
- 后端 `web/` ≥ 80%
- 前端关键组件单测齐全（不强求行覆盖率），人工 UI 验收为主

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents web start` 后 `curl http://127.0.0.1:8765/api/health` 返回 200 + 完整 JSON | shell |
| A2 | 浏览器打开 `http://127.0.0.1:8765` 加载 SPA，5 个页面均可路由打开 | 人工 |
| A3 | 非 localhost 来源访问被 403 | 远程 curl 测试 |
| A4 | 设置 `AKQ_WEB_TOKEN` 后无 token 请求被 401，正确 token 通过 | curl |
| A5 | Portfolio 页选择历史日期能展示对应组合 + 归因 + 行业分布 | 人工 |
| A6 | Factors 页切换因子能看到 IC 走势 + IR 序列 + 衰减曲线 | 人工 |
| A7 | Jobs 页能看到当日 batch.post_close 时间线 + 点击可看 payload | 人工 |
| A8 | Chat 页发一条问题能 SSE 流式收到 LLM 回复，且工具调用步骤可见 | 人工 |
| A9 | daemon 在跑、web 在跑，互不阻塞；web 不修改业务表（仅 chat_messages） | 监控 sqlite 文件锁 |
| A10 | 仅 GET 端点为业务 endpoint；任何 POST/PUT/DELETE 都属于 /api/chat/* | grep + audit |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/web/` 后端覆盖率 ≥ 80% | `pytest --cov` |
| B2 | 前端 `vitest run` 全绿 | npm test |
| B3 | `ruff check src/akq_agents/web/` 零警告 | CI |
| B4 | 前端 `tsc --noEmit` 零类型错误 | CI |
| B5 | OpenAPI schema（FastAPI 自动生成 `/api/openapi.json`）可用 | curl |
| B6 | 鉴权失败时不泄露 stacktrace；只返回简短错误 | 测试 |

### C. 性能验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | `/api/health` P95 ≤ 200ms | wrk/ab |
| C2 | `/api/portfolio?date=...` 4000 标的快照下 ≤ 500ms | 实测 |
| C3 | SPA 首屏 LCP ≤ 2s（本地） | Lighthouse |
| C4 | chat SSE 首 token 延迟 ≤ 3s（LLM 网关正常时） | 人工掐表 |
| C5 | web 进程常驻 RSS ≤ 200MB（不含 LLM 调用峰值） | ps |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/web_console.md`：架构、API 列表、鉴权、部署、故障排查、截图 |
| D2 | `web/README.md`：前端构建/开发指引、组件结构 |
| D3 | README 增加 `web start` 命令 + 浏览器访问指引 |
| D4 | OpenAPI 文档可在 `/api/docs` 浏览（FastAPI Swagger UI） |

### 里程碑参考

- M5.1 后端骨架（FastAPI app + auth + ServiceContainer + /health endpoint）（1–2 天）
- M5.2 Portfolio / Factors / Jobs 三组 API + schema + 测试（2–3 天）
- M5.3 Chat API 含 SSE 流（接 P4 LLMOrchestrator）（1–2 天）
- M5.4 前端骨架（Vite + Tailwind + Router + 5 页占位 + auth）（1 天）
- M5.5 Dashboard / Portfolio / Factors 三页（含 ECharts）（2–3 天）
- M5.6 Jobs / Chat 两页（SSE 流渲染）（1–2 天）
- M5.7 构建打包 + CLI `web start/stop` + 端到端 smoke（1 天）
- M5.8 文档、性能压测、UI 打磨（1 天）

**预估总工时：10–14 工作日。**

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| sqlite 并发 daemon 写 / web 读冲突 | 卡住或读到中间状态 | 启用 WAL（P1 已开），所有 web 读用短事务 |
| FastAPI 嵌入 SPA 静态文件路径错位 | 部署失败 | 测试覆盖：dev 模式 / wheel 打包模式两套路径策略 |
| SSE 在某些代理后掉线（如未来加反向代理） | chat 体验差 | 心跳 + 重连；记录会话使客户端 reload 后能拉历史 |
| LLM 流接口尚未在 P4 完成 | chat 退化到非流式 | 提供 fallback：等完整响应再 send 一次 SSE done |
| 鉴权失误暴露到外网 | 安全事故 | 启动期校验 bind_host 必为 loopback；CLI 拒绝任何 0.0.0.0 |
| ECharts 大数据点渲染卡 | factors 页 250 点 + 多窗口 | 数据预下采样到 ≤ 250 点；横向滚动而非缩放 |
| 前端构建产物未被 python wheel 包含 | 安装后 404 | pyproject `package-data` 显式包含 `web/static/**` |

### 越界声明

- ❌ 多用户 / 权限矩阵
- ❌ 任何对外网暴露
- ❌ Web 上修改配置 / 编辑因子代码 / 触发交易
- ❌ 移动端 App
- ❌ 实时分钟级 K 线
- ❌ 自动告警（P6）

---

## 附录 A：与 P1/P2/P3/P4 接口契约

P5 是**纯展示层**，严格只读消费：

1. `DataHealth` schema（P1）— 渲染数据健康卡片。
2. `meta.db.fetch_errors` 表（P1）— Jobs 页"未解决错误"统计。
3. `daemon_state`、`job_runs`、`events` 表（P2）— Dashboard / Jobs。
4. `factor_metrics`、`portfolio_snapshots` 表（P3）— Factors / Portfolio。
5. `attribution.json` schema（P3）— Portfolio 归因。
6. `LLMOrchestrator.run()` 接口（P4）— Chat 后端调用入口；P5 不另起一套 LLM 链路。
7. `llm_calls` / `chat_sessions` / `chat_messages` 表（P4）— Chat 页历史。

P5 **不能修改**上述任何表或接口；如发现需求要改，必须回到对应阶段补丁。

## 附录 B：与 P6 接口承诺

- `/api/health` 响应 schema 稳定 → P6 监控/告警系统直接 scrape。
- 鉴权策略文档化：P6 容器化时可选注入 token；如要对外开放需在 P6 增加反向代理 + TLS（非 P5 范畴）。
- 前端构建产物路径稳定（`src/akq_agents/web/static/`）：P6 Dockerfile 不需做额外特化。
