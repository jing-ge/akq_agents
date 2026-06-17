# P5 Web 控制台 — 设计文档（v2，oracle review 后收敛）

- 项目：akq-agents
- 阶段：P5（共 P1–P6 六阶段中的第五阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataHealth / 缓存 / WAL）、P2（job_runs / events / daemon_state.json / events.kind 规范）、P3（factor_metrics / portfolio_snapshots / attribution）、P4（llm_calls / chat_messages / LLMOrchestrator.run_chat_turn）

> **v2 收敛说明**（oracle review 后）：
> - **前端栈降级为 FastAPI + Jinja2 + HTMX + ECharts (CDN)**，砍掉 React/Vite/Tailwind/React Query/TypeScript 全套工程。3-5 天足够，原 10-14 天太重。
> - **5 页合并为 3 页**：Ops（Dashboard + Jobs 合并）、Research（Portfolio + Factors 合并）、Chat。
> - **砍掉鉴权层**：localhost-only 已是天然 air gap；启动期硬校验 `bind_host ∈ {127.0.0.1, localhost, ::1}`，不允许 0.0.0.0。
> - **砍掉 `web stop/status` CLI**：Ctrl+C 即可停；status 没意义。
> - **砍掉 `/api/docs` Swagger**、`/api/portfolio/history`（YAGNI）。
> - **明确 `uvicorn --workers 1`**：lru_cache 单例 ServiceContainer 假设单 worker。
> - **SSE 不流式**：P4 不实现 streaming，P5 直接"等完整响应再 send 一次 SSE done"，不做 token 增量渲染（P4 附录 B §4 已明文承诺）。
> - **API 全 GET**（除 chat POST）；写业务表的 API 一律不存在。

---

## §1 目标与边界

### 目标

把分散在 CLI / sqlite / markdown / parquet 里的状态聚合成一个**本地浏览器可访问的轻量 Web 控制台**：

- Ops 页：系统状态 + 任务历史 + 实时事件（合并原 Dashboard + Jobs）。
- Research 页：组合详情 + 因子有效性（合并原 Portfolio + Factors）。
- Chat 页：用 P4 LLMOrchestrator.run_chat_turn 跑对话。
- 全 localhost；纯只读消费 P1-P4。

### 在做什么（P5 范围）

- FastAPI 后端，复用 P1-P4 的服务（不重新实现业务）。
- 3 个核心 API 命名空间：`/ops`、`/research`、`/chat`。
- Jinja2 模板 + HTMX 局部刷新 + ECharts CDN 出图（3 页面 + 全局 base layout）。
- SSE：`/api/chat/sessions/{sid}/messages` 仅一个 SSE 端点，非流式，等 P4 返回后一次性 send。
- 启动期硬校验 bind_host 是 loopback；非 localhost 来源 → 403。
- 单端口：FastAPI 直接 mount 静态资源 + Jinja，`http://127.0.0.1:8765`。
- CLI：`akq-agents web start`（仅一条命令）。

### 不在做什么

- ❌ React / Vite / Tailwind / TypeScript / npm — 不引入前端工程链。
- ❌ 多用户 / 团队协作 / 权限矩阵 — 单用户假设。
- ❌ 鉴权 token / 登录页 — localhost-only 已足够。
- ❌ 对外网开放（启动期硬校验阻止 0.0.0.0）。
- ❌ 用户在 Web 上修改配置 / 编辑因子代码。
- ❌ 自动下单 / 委托 / 交易终端集成（永远禁止，与 P4 一致）。
- ❌ 移动端独立 App / 响应式深度优化。
- ❌ 重型前端构建工具链。
- ❌ 实时分钟级行情图。
- ❌ Streaming token 增量渲染。
- ❌ OpenAPI / Swagger UI。
- ❌ CLI `web stop` / `web status`（Ctrl+C 即可）。
- ❌ `/api/portfolio/history` 趋势线（YAGNI）。

---

## §2 架构

### 整体拓扑

```
┌────────────────────────────────────────────────────────────────┐
│  Browser (http://127.0.0.1:8765)                                 │
│  - Jinja2 渲染的 HTML                                            │
│  - HTMX：表格 / 卡片 局部刷新（轮询 5s）                          │
│  - ECharts (CDN)：组合饼图 / IC 折线 / 任务时间轴                 │
│  - SSE for chat：单端点，等完整响应后 send done                  │
└────────────┬───────────────────────────────────────────────────┘
             │ HTTP / SSE (同源单端口)
             ▼
┌────────────────────────────────────────────────────────────────┐
│  FastAPI App  (uvicorn --workers 1, 强制)                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  pages/ （Jinja 返回 HTML）                                │  │
│  │  ├─ GET /                  → redirect /ops                 │  │
│  │  ├─ GET /ops               → 整页 ops.html                 │  │
│  │  ├─ GET /research          → 整页 research.html            │  │
│  │  └─ GET /chat              → 整页 chat.html                │  │
│  │                                                            │  │
│  │  api/ （JSON 或 HTMX 局部 HTML 片段）                       │  │
│  │  ├─ GET  /api/ops/health           （JSON）                 │  │
│  │  ├─ GET  /api/ops/job-runs         （HTMX 片段 or JSON）    │  │
│  │  ├─ GET  /api/ops/events           （HTMX 片段 or JSON）    │  │
│  │  ├─ GET  /api/research/portfolio?date=...                  │  │
│  │  ├─ GET  /api/research/portfolio/attribution?date=...      │  │
│  │  ├─ GET  /api/research/factors                             │  │
│  │  ├─ GET  /api/research/factors/{name}/metrics              │  │
│  │  ├─ POST /api/chat/sessions                                │  │
│  │  ├─ GET  /api/chat/sessions                                │  │
│  │  ├─ POST /api/chat/sessions/{sid}/messages (SSE)            │  │
│  │  └─ GET  /api/chat/sessions/{sid}/messages                 │  │
│  ├─ deps.py        @lru_cache ServiceContainer（worker=1 前提）  │  │
│  ├─ guard.py       startup hook: 校验 bind_host 是 loopback     │  │
│  ├─ templates/     Jinja2 模板（pages + 片段）                   │  │
│  └─ static/        ECharts CDN fallback / 一点 css              │  │
└────────────┬───────────────────────────────────────────────────┘
             │ 服务调用（进程内）
             ▼
┌────────────────────────────────────────────────────────────────┐
│  现有服务层（P1–P4 复用，零修改）                                  │
│  - DataRepository (P1)                                            │
│  - SchedulerStateStore / DaemonStateFile (P2)                      │
│  - FactorRegistry / portfolio_snapshots (P3)                       │
│  - LLMOrchestrator.run_chat_turn (P4)                              │
└────────────────────────────────────────────────────────────────┘
```

### 进程模型

**单进程**：`akq-agents web start` 拉起一个 uvicorn worker（**强制 workers=1**）；该进程 import 所有现有服务（与 daemon 进程**完全独立**，只共享 sqlite/parquet 数据文件）。

- **不与 daemon 共进程**：daemon 是 batch-heavy 长跑，web 是请求-响应。
- **sqlite WAL**：P1 附录 B §6 已强制承诺 WAL + busy_timeout=5000；P5 不再自己设置。
- **进程间通讯**：通过 sqlite 表 + parquet 文件 + `data/daemon_state.json`，零 IPC。

### 安全（轻量化）

```python
# guard.py 启动期硬校验
def assert_loopback_bind(host: str):
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(f"P5: bind_host 必须为 loopback (得到 {host})")

# middleware: 拒绝非本地来源（防止反向代理意外暴露）
async def require_localhost(request: Request, call_next):
    if request.client.host not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"error": "non-local request rejected"}, status_code=403)
    return await call_next(request)
```

> **不做 Bearer token**：单用户本地，"猫踩键盘"风险论证太弱（oracle 认可）。

### 单端口部署

```python
# server.py
app.add_middleware(BaseHTTPMiddleware, dispatch=require_localhost)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(pages_router)                  # /, /ops, /research, /chat
app.include_router(api_router, prefix="/api")
```

**静态资源**：ECharts 用 CDN（`https://cdn.jsdelivr.net/npm/echarts/dist/echarts.min.js`）；fallback 本地副本放 `src/akq_agents/web/static/vendor/echarts.min.js`，CDN 不通时模板 `{% if ECHARTS_LOCAL %}` 切回本地。

### 配置示例（`config/web.yaml`，新增）

```yaml
web:
  bind_host: "127.0.0.1"
  bind_port: 8765
  poll_intervals_ms:
    ops_health: 5000        # ops 页 health 卡片
    ops_jobs: 5000          # ops 页 job_runs 表
    ops_events: 3000        # ops 页 events 流
  chat:
    sse_keepalive_s: 15
    max_message_chars: 4000
  ui:
    title: "AKQ Agents Console"
    timezone: "Asia/Shanghai"
  echarts:
    use_cdn: true           # false 时切到本地 vendor/echarts.min.js
```

---

## §3 数据流与时序

### 流程 1：Ops 页（合并 Dashboard + Jobs）

```
GET /ops
  → 渲染 ops.html (整页)
  → 页内 4 个 HTMX 区块每 N 秒轮询：
       - #health   (5s)  → GET /api/ops/health (JSON 或 HTML 片段)
       - #jobs     (5s)  → GET /api/ops/job-runs?limit=50
       - #events   (3s)  → GET /api/ops/events?limit=50&level_min=info
       - 整体 daemon-online 状态条由 health 端点返回

GET /api/ops/health
  → 聚合：
       - DataRepository.quality_report()          → DataHealth (P1)
       - DaemonStateFile.read() + is_alive()      → daemon 状态
       - 最近一次 batch.post_close 的 job_run 摘要
       - 最近 24h scheduler events 按 level 统计（注意：与 P1.DataHealth.unresolved_errors_24h 字段语义不同）
  → 返回:
     {
       "data_health": {...},              # P1 DataHealth, 含 unresolved_errors_24h（fetch_errors 维度）
       "daemon": {"status": "running", "last_heartbeat": "...", "is_alive": true},
       "today_batch": {"status": "ok", "started_at": "...", "duration_ms": ...},
       "scheduler_events_24h_by_level": {"info": 102, "warning": 3, "error": 0}
     }

GET /api/ops/job-runs?limit=50&job_id=*
  → SchedulerStateStore.list_recent_runs(...)   (P2)
  → HTMX 模式：返回 jobs_table.html.j2 片段；JSON 模式：返回 list[JobRun]

GET /api/ops/events?limit=50&level_min=info&since=...
  → SchedulerStateStore.list_events(...)
  → 类似 HTMX 片段 / JSON 模式
```

> **字段命名规范**：`data_unresolved_errors_24h` 来自 P1 fetch_errors，`scheduler_events_24h_by_level` 来自 P2 events 表，**严格区分**避免 UI 混淆。

### 流程 2：Research 页（合并 Portfolio + Factors）

```
GET /research
  → 渲染 research.html (整页) 含日期选择 + 因子下拉
  → 页内区块：
       - #portfolio-table   GET /api/research/portfolio?date=...
       - #attribution-chart GET /api/research/portfolio/attribution?date=...
       - #factor-list       GET /api/research/factors
       - #factor-detail     GET /api/research/factors/{name}/metrics?limit=120
       (HTMX 切换日期 / 因子时局部刷新)

GET /api/research/portfolio?date=2026-06-17
  → SELECT * FROM portfolio_snapshots WHERE as_of_date=? ORDER BY weight DESC
  → 直接消费 P3 已写入的字段：name / industry / top_factors_json / prev_weight
  → 不需要 join P1 universe 或行业表（P3 附录 B §1 承诺）
  → 返回:
     {
       "as_of_date": "2026-06-17",
       "rows": [{symbol, name, industry, weight, prev_weight, composite_score, top_factors}],
       "industry_breakdown": [{industry, total_weight}],   # 内存聚合 rows
       "turnover": 0.18,                                    # = sum(|w - prev|)/2
       "summary": "..."
     }
  特殊路径：
    - date 早于任何 portfolio_snapshots 记录 → 404 + body {"error": "no_snapshot_for_date"}
    - date 是未来 → 同上

GET /api/research/portfolio/attribution?date=2026-06-17
  → 优先从 portfolio_snapshots 内存聚合 portfolio_contribution（与 reports/<date>/attribution.json 同源）
  → fallback 读 reports/<date>/attribution.json（若 portfolio_snapshots 为空但文件存在）

GET /api/research/factors
  → FactorRegistry.list_all() + factor_metrics 最近一次（按 factor_version 取最新）
  → 返回:
     [{name, factor_version, direction, lookback_days,
       last_metric: {as_of_date, ic, ir, status} | null}]

GET /api/research/factors/{name}/metrics?limit=120
  → SELECT * FROM factor_metrics
     WHERE factor_name=? ORDER BY factor_version DESC, as_of_date DESC LIMIT ?
  → ECharts 折线图渲染
  → 注意：因 factor_version 升级会断层，前端按 version 分组显示（不同颜色或副 axis）
```

### 流程 3：Chat 页

```
GET /chat
  → 渲染 chat.html (整页)，左侧 session 列表，右侧消息流
  → 页加载时：
       GET /api/chat/sessions  → 已有 session 列表（按 session_id 聚合 chat_messages）

POST /api/chat/sessions  Body: {}
  → 调 P4 LLMStore 生成一个新 session_id；
     写入第一条 chat_messages(role="system", content=load_prompt("chat_system"))
  → 返回 {session_id}

POST /api/chat/sessions/{sid}/messages
  Body: {"content": "...", "model": "Claude-Opus-4.7"}
  Content-Type: text/event-stream
  → 阻塞调用 P4 LLMOrchestrator.run_chat_turn(...)
       （**非流式**，等完整响应后再 SSE）
  → SSE 输出：
       event: tool_use   data: {"name": "...", "args": {...}, "result": {...}}    # 每个工具一行（按顺序）
       event: assistant  data: {"content": "..."}                                  # 完整文本一次性发
       event: done       data: {"message_id": ..., "iterations": N}
  → 中途异常 → event: error data: {"message": "..."}
  → 心跳：每 15 秒发 `: keepalive` 注释行

GET /api/chat/sessions/{sid}/messages?limit=200
  → SELECT * FROM chat_messages WHERE session_id=? ORDER BY ts ASC LIMIT ?
  → 用于打开页面或恢复会话
```

> **模型选择**：Chat 页右上角的"模型选择器"P5 不实现（v2 P4 仅 Anthropic Claude-Opus-4.7）；模型固定为该模型，前端不暴露切换。

### 流程 4：部分系统宕机时的渲染策略

- **daemon 未启动**：`/api/ops/health` 仍能返回，`daemon.is_alive=false`，`today_batch=null`；ops 页头部条显示红色"调度守护未运行"提示。
- **P3 没跑过任何 batch**：`portfolio_snapshots` 为空。Research 页选择日期时 → 404 与"暂无组合快照"友好提示。
- **P4 LLM 网关 down**：Chat 页 POST → SSE event=error；UI 显示"LLM 网关暂时不可用"。
- **factor_metrics 为空**：Research 因子页 last_metric=null，前端显示 "-"。

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── web/                              ← 新增子包
│   ├── __init__.py
│   ├── server.py                     # uvicorn 启动入口（workers=1 强制 + assert_loopback_bind）
│   ├── app.py                        # FastAPI 实例 + middleware + 路由挂载
│   ├── deps.py                       # @lru_cache ServiceContainer (P1–P4 单例)
│   ├── guard.py                      # assert_loopback_bind + require_localhost middleware
│   ├── pages.py                      # GET / /ops /research /chat → Jinja 整页
│   ├── api/
│   │   ├── __init__.py
│   │   ├── ops.py                    # /api/ops/health|job-runs|events
│   │   ├── research.py               # /api/research/portfolio*|factors*
│   │   └── chat.py                   # /api/chat/sessions* (含 SSE)
│   ├── schemas/                      # FastAPI 响应 schema（pydantic v2）
│   │   ├── ops.py
│   │   ├── research.py
│   │   └── chat.py
│   ├── templates/                    ← Jinja2
│   │   ├── base.html.j2              # layout + nav + ECharts CDN script
│   │   ├── ops.html.j2
│   │   ├── research.html.j2
│   │   ├── chat.html.j2
│   │   └── fragments/                # HTMX 局部片段
│   │       ├── jobs_table.html.j2
│   │       ├── events_list.html.j2
│   │       ├── portfolio_table.html.j2
│   │       └── factor_list.html.j2
│   └── static/
│       ├── app.css                   # 一点点全局样式（< 200 行）
│       ├── htmx.min.js               # CDN fallback
│       └── vendor/echarts.min.js     # CDN fallback
└── cli/
    └── app.py                        # 新增子命令：web start (仅一条)
```

> 不需要 `web/`（项目根级）独立前端目录；不需要 `package.json` / `vite.config.ts` / `tsconfig.json` / `vitest`。

### 关键 API 响应 Schema（pydantic v2）

```python
# schemas/ops.py
class OpsHealthResponse(BaseModel):
    data_health: DataHealth         # 复用 P1 schema（含 unresolved_errors_24h，fetch_errors 维度）
    daemon: DaemonStatus            # 复用 P2 schema（is_alive + last_heartbeat）
    today_batch: JobRunSummary | None
    scheduler_events_24h_by_level: dict[str, int]

# schemas/research.py
class PortfolioResponse(BaseModel):
    as_of_date: date
    rows: list[PortfolioRow]              # 直接 1:1 映射 portfolio_snapshots 字段
    industry_breakdown: list[IndustryWeight]
    turnover: float
    summary: str

class FactorListItem(BaseModel):
    name: str
    factor_version: int
    direction: Literal["long", "short"]
    lookback_days: int
    last_metric: FactorMetric | None       # None 表示首次未跑

# schemas/chat.py
class ChatStreamEvent(BaseModel):
    event: Literal["tool_use", "assistant", "done", "error"]
    data: dict
```

### Service Container 依赖注入

```python
# web/deps.py（启动时构造一次，复用）
@lru_cache(maxsize=1)
def get_services() -> ServiceContainer:
    """前提：uvicorn --workers 1。multi-worker 会破坏单例假设。"""
    config = load_all_configs()
    return ServiceContainer(
        repo=DataRepository(...),                  # P1
        sched_store=SchedulerStateStore(...),      # P2
        daemon_state=DaemonStateFile(...),         # P2
        factor_registry=FactorRegistry(...),       # P3
        portfolio_store=PortfolioSnapshotStore(...),  # P3
        llm_orchestrator=LLMOrchestrator(...),     # P4
        llm_store=LLMStore(...),                   # P4
    )

@router.get("/api/ops/health")
async def health(svc: ServiceContainer = Depends(get_services)):
    return OpsHealthResponse(...)
```

### 启动期 guard

```python
# server.py
def start(host: str = "127.0.0.1", port: int = 8765):
    from .guard import assert_loopback_bind
    assert_loopback_bind(host)
    import uvicorn
    uvicorn.run("akq_agents.web.app:app",
                host=host, port=port,
                workers=1,           # 硬编码，不暴露配置
                log_level="info")
```

### 模板栈

- **Jinja2**：自带的 FastAPI `Jinja2Templates`。
- **HTMX**：CDN `https://unpkg.com/htmx.org@1.9.x`；本地 fallback `static/htmx.min.js`。一段 `hx-get="/api/ops/events?limit=50" hx-trigger="every 3s" hx-target="#events"` 即可实现轮询。
- **ECharts**：CDN 同上；fallback 同上。每个图表 < 50 行 inline `<script>`。
- **SSE**：HTMX 1.9 自带 `hx-sse` 扩展；不行就 vanilla `EventSource` + 简短 JS。

### 关键边界

- ❌ 不实现 SSR 路由 / 静态站点生成。
- ❌ 不引入 React / Vue / Svelte。
- ❌ 不引入前端测试工具链（vitest / playwright）。
- ❌ 不做用户管理 / 注册 / token。
- ❌ Web 进程不写业务表（chat_messages 由 P4 LLMOrchestrator 写，不算 P5 写）。
- ❌ 不嵌入富文本编辑器 / Notebook。
- ❌ 不实现实时 K 线图（无分钟级数据）。
- ❌ 不支持多 worker（启动期硬编码 workers=1）。
- ❌ 不暴露模型切换 UI（v2 P4 单模型）。

### 测试策略

```
tests/web/
├── test_guard.py                  # assert_loopback_bind raise on 0.0.0.0
├── test_localhost_middleware.py   # 非 localhost 来源 → 403
├── test_api_ops_health.py         # mock services → 检验 schema 字段命名
├── test_api_ops_jobs.py
├── test_api_ops_events.py
├── test_api_research_portfolio.py # 直接消费 portfolio_snapshots，不 join
├── test_api_research_factors.py   # factor_version 分组返回
├── test_api_chat_session.py
├── test_api_chat_sse.py           # SSE 完整事件序列：tool_use → assistant → done
├── test_pages.py                  # /, /ops, /research, /chat 200 返回 HTML
└── conftest.py                    # FastAPI TestClient + 假 ServiceContainer
```

目标覆盖率：**后端 `web/` ≥ 80%**；无前端测试。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents web start` 后 `curl http://127.0.0.1:8765/api/ops/health` 返回 200 + 完整 JSON | shell |
| A2 | 浏览器访问 `http://127.0.0.1:8765` 重定向到 `/ops`；3 个页面（/ops, /research, /chat）均可打开 | 人工 |
| A3 | 非 localhost 来源访问任一端点被 403 | 远程 curl 测试 |
| A4 | 启动时 `bind_host=0.0.0.0` 直接 SystemExit 拒启 | 单测 |
| A5 | Research 页选择有快照的历史日期能展示组合 + 归因 + 行业分布 | 人工 |
| A6 | Research 页选择无快照的日期得到友好提示（404 + "暂无组合快照"） | 人工 |
| A7 | Research 因子页切换因子能看到 IC / IR 折线，factor_version 分组显示 | 人工 |
| A8 | Ops 页能看到当日 batch.post_close 时间线 + 点击可看 payload | 人工 |
| A9 | Chat 页发一条问题能收到 SSE 完整事件序列：N 个 tool_use → 1 个 assistant → 1 个 done | 人工 + 单测 |
| A10 | daemon 未启动时 ops 页头部条显示"调度守护未运行"，不崩页 | 人工（停掉 daemon 测） |
| A11 | API schema：`data_health.unresolved_errors_24h` 与 `scheduler_events_24h_by_level` 不混用 | 单测 |
| A12 | uvicorn 启动若指定 `--workers 2` 会被启动入口覆盖回 1（或拒启） | 单测 |
| A13 | 所有业务相关 API 仅 GET；POST 仅限 `/api/chat/*` | grep + 单测 |
| A14 | Portfolio API 不查 P1 universe 表来拼 name（验证消费 portfolio_snapshots.name 字段） | 单测 mock |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/web/` 后端覆盖率 ≥ 80% | `pytest --cov` |
| B2 | `ruff check src/akq_agents/web/` 零警告 | CI |
| B3 | 鉴权失败 / 异常时不泄露 stacktrace；只返回简短错误 | 单测 |
| B4 | 无 React / Vite / npm 相关依赖出现在 `requirements.txt` / `pyproject.toml` | 文件检查 |

### C. 性能验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | `/api/ops/health` P95 ≤ 200ms | wrk/ab |
| C2 | `/api/research/portfolio?date=...` 500 标的（P3a universe） ≤ 200ms | 实测 |
| C3 | 整页首屏（含 ECharts 渲染） ≤ 1.5s | 人工掐表 |
| C4 | SSE 首事件延迟 ≤ 等 P4 LLM 返回的时长 + 200ms | 人工 |
| C5 | web 进程常驻 RSS ≤ 200MB（不含 LLM 调用峰值） | ps |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/web_console.md`：架构、API 列表、安全模型（localhost-only）、部署、故障排查、截图 |
| D2 | README 增加 `web start` 命令 + 浏览器访问指引 |
| D3 | 明确记录：HTMX + Jinja + ECharts 选型；无前端工程链 |

### 里程碑参考

- M5.1 后端骨架（FastAPI app + guard + middleware + ServiceContainer + ops health）（1 天）
- M5.2 ops / research API + 模板 + HTMX 轮询（1.5 天）
- M5.3 chat API 含 SSE（接 P4 LLMOrchestrator.run_chat_turn）（1 天）
- M5.4 ECharts 集成（任务时间轴、组合饼图、IC 折线）（0.5–1 天）
- M5.5 CLI `web start` + 文档 + 端到端 smoke（0.5–1 天）

**预估总工时：4–5 工作日**（v1 的 10–14 天因换 HTMX 栈、合并页面、砍鉴权/Swagger/CLI/`/api/portfolio/history` 而大幅缩短）。

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| sqlite 并发 daemon 写 / web 读冲突 | 卡住或读到中间状态 | P1 已承诺 WAL + busy_timeout；web 用短只读事务 |
| daemon 没启动时 ops 页空数据 | UI 误导 | health 端点返回 `is_alive=false`；模板友好提示 |
| ECharts CDN 不通（离线 / 内网） | 图表挂 | 本地 vendor fallback + 配置开关 |
| SSE 在某些代理后掉线 | chat 体验差 | P5 不假设有反向代理；如真加 P6 提供 sticky session |
| 一旦未来真要多 worker | ServiceContainer 单例失效 | 启动入口硬编码 workers=1，文档警告；多 worker 留 P6 |
| HTMX 学习曲线 | 上手慢 | 每个轮询不超过 1 个属性；docs/web_console.md 给 cheatsheet |
| events.kind 飘移使前端筛选崩 | UI 显示混乱 | 复用 P2 附录 C 的 enum；前端不硬编码 kind 字符串 |

### 越界声明

- ❌ 多用户 / 权限矩阵
- ❌ 对外网暴露
- ❌ Web 上修改配置 / 编辑因子代码 / 触发交易
- ❌ 移动端 App
- ❌ 实时分钟级 K 线
- ❌ Streaming token 增量渲染
- ❌ React / Vue / SPA 工程化
- ❌ OpenAPI / Swagger UI
- ❌ CLI `web stop` / `web status`

---

## 附录 A：与 P1/P2/P3/P4 接口契约

P5 是**纯展示层**，严格只读消费：

1. `DataHealth` schema（P1）— `/api/ops/health` 渲染数据健康卡片。
2. `meta.db.fetch_errors` 表（P1）— Ops 页通过 `DataHealth.unresolved_errors_24h` 间接展示。
3. `data/daemon_state.json` 文件（P2）— Ops 页 daemon 在线判定。
4. `job_runs` 表（P2）— Ops 页任务历史。
5. `events` 表 + P2 附录 C 的 kind enum（P2）— Ops 页事件流；P5 前端严格按 enum 渲染颜色 / 分类。
6. `factor_metrics` 表，按 `(factor_name, factor_version, as_of_date)` 取最新（P3）。
7. `portfolio_snapshots` 表（P3，含 `name` / `industry` / `top_factors_json` 字段）— **P5 直接 SELECT 渲染，不 join 其他表**（P3 附录 B §1 承诺）。
8. `reports/<date>/attribution.json`（P3）— 作为 `portfolio_snapshots` 的镜像 fallback。
9. `LLMOrchestrator.run_chat_turn()`（P4）— Chat 后端调用入口；**P5 不另起一套 LLM 链路**。
10. `chat_messages` 表（P4）— Chat 页历史。
11. `meta.db` WAL（P1 附录 B §6）— 多进程并发安全。
12. **`events.kind` 仅消费 P2 附录 C 已枚举的集合**；前端遇到未知 kind → 当作 `info` 处理 + 显示原始字符串。

P5 **不能修改**上述任何表或接口；如发现需求要改，必须回到对应阶段补丁。

## 附录 B：与 P6 接口承诺

- `/api/ops/health` 响应 schema 稳定 → P6 监控/告警系统直接 scrape。
- 鉴权策略文档化：P6 容器化时如需对外开放需在 P6 增加反向代理 + TLS（非 P5 范畴）。
- 模板路径稳定（`src/akq_agents/web/templates/`）：P6 Dockerfile 不需做额外特化。
- 启动入口 `akq_agents.web.server.start()` 函数签名稳定（P6 可在 systemd unit 中调用）。