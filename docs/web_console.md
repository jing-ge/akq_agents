# P5 Web 控制台使用指南

> 对应设计文档：`docs/superpowers/specs/2026-06-17-p5-web-console-design.md`

## 1. 模块总览

```
src/akq_agents/web/
├── __init__.py
├── server.py                   # uvicorn 启动入口（workers=1 硬编码）
├── app.py                      # FastAPI app + middleware + 3 个页面
├── deps.py                     # ServiceContainer + @lru_cache
├── guard.py                    # assert_loopback_bind + LocalhostOnlyMiddleware
├── api/
│   ├── ops.py                  # /api/ops/health|job-runs|events
│   ├── research.py             # /api/research/portfolio*|factors*
│   └── chat.py                 # /api/chat/sessions* (含 SSE)
├── templates/                  # Jinja2 模板
│   ├── base.html.j2            # 全局 layout + ECharts CDN
│   ├── ops.html.j2
│   ├── research.html.j2
│   └── chat.html.j2
└── static/                     # 占位；无 vendor 资源
```

入口配置：`config/web.yaml`

## 2. 设计要点（spec v2）

- **localhost-only**：启动期硬校验 bind_host 是 loopback；middleware 拒绝非本地来源 → 403
- **无鉴权**：localhost 已是天然 air gap；不引入 token
- **HTMX + Jinja + ECharts CDN**：无前端工程链；3 页面合计 < 800 行代码
- **`uvicorn workers=1` 强制**：与 `@lru_cache` ServiceContainer 单例假设一致
- **SSE 非流式**：与 P4 承诺一致 — 等 LLM 完整响应后一次性 send tool_use / assistant / done 事件
- **OpenAPI 已禁用**（`docs_url=None, openapi_url=None`）

## 3. CLI 速查

```bash
# 启动（前台阻塞，Ctrl+C 退出）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app web start

# 自定义端口
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app web start --port 8888

# 浏览器访问
open http://127.0.0.1:8765/
```

启动后 3 个页面：
- `http://127.0.0.1:8765/ops` — 系统状态 + 任务历史 + 事件流
- `http://127.0.0.1:8765/research` — 组合 + 因子有效性
- `http://127.0.0.1:8765/chat` — LLM 对话（需 P4 LLM 网关在跑）

## 4. API 端点

### Ops

| 端点 | 描述 |
|---|---|
| `GET /api/ops/health` | 聚合 DataHealth + DaemonState + today_batch + 24h events 统计 |
| `GET /api/ops/job-runs?limit=&job_id=&status=` | 最近任务运行 |
| `GET /api/ops/events?limit=&level_min=&kind_prefix=` | 事件流 |

### Research

| 端点 | 描述 |
|---|---|
| `GET /api/research/portfolio?date=YYYY-MM-DD` | 组合快照（404 if no snapshot） |
| `GET /api/research/portfolio/attribution?date=YYYY-MM-DD` | 归因聚合 |
| `GET /api/research/factors` | 所有因子 + 最近 metrics |
| `GET /api/research/factors/{name}/metrics?limit=` | 因子历史 metrics |

### Chat

| 端点 | 描述 |
|---|---|
| `GET /api/chat/sessions?limit=` | 列出最近 sessions |
| `POST /api/chat/sessions` | 创建新 session |
| `GET /api/chat/sessions/{sid}/messages?limit=` | 拉取历史 |
| `POST /api/chat/sessions/{sid}/messages` | SSE 流；body: `{content, model?}` |

## 5. 安全模型

1. **bind_host 启动期校验**：`assert_loopback_bind`；非 `127.0.0.1/localhost/::1` 直接 `sys.exit(2)`
2. **LocalhostOnlyMiddleware**：每个请求检查 client.host；非 loopback → 403
3. **API 仅 GET**（除 chat POST）：所有写操作不可能从 web 触发
4. **chat 工具仍 read-only**（P4 已强校验）：LLM 不能下单
5. **无 OpenAPI**：避免泄露 schema

## 6. 表 / 文件依赖

P5 是**纯展示层**，读以下数据：

| 来源 | 说明 |
|---|---|
| `meta.db.fetch_errors` (P1) | 通过 DataHealth.unresolved_errors_24h 间接展示 |
| `data/daemon_state.json` (P2) | Daemon 在线判定 |
| `meta.db.job_runs` (P2) | Ops 任务历史 |
| `meta.db.events` (P2) | Ops 事件流 |
| `meta.db.factor_metrics` (P3) | Factor 详情页 |
| `meta.db.portfolio_snapshots` (P3) | Portfolio 表格 + 归因（直接 SELECT，不 join 其他表） |
| `meta.db.llm_calls` (P4) | 暂未直接渲染（保留扩展） |
| `meta.db.chat_messages` (P4) | Chat 页历史 |
| `LLMOrchestrator.run_chat_turn` (P4) | Chat POST 端点直接调用 |

## 7. 故障排查

### 启动失败 `bind_host 必须为 loopback`
- 检查 `config/web.yaml.bind_host`；不要写 `0.0.0.0`

### 浏览器打开页面看到 "调度守护未运行"
- daemon 没在跑或 `daemon_state.json` 没创建；这是正常提示，不是 web 故障

### `/api/research/portfolio?date=...` 返回 404
- 该日期 P3 没跑过 batch；先 `daemon start` 跑一次，或手动调 `PortfolioAgent`

### Chat 页 SSE 没回应
- 检查 `http://127.0.0.1:18931/` 是否在跑（P4 LLM 网关）
- 用 `llm calls --last 5` 看最近调用 status

### ECharts 图不显示
- 检查 CDN 网络（`config/web.yaml.echarts.cdn_url`）；离线时设 `use_cdn=false` + 本地 vendor（v1 未实现 fallback，可手动下载放 `web/static/vendor/echarts.min.js`）

## 8. 验收快查

| Spec | 状态 | 验证方式 |
|---|---|---|
| A1 `web start` 后 health 200 | ✅ | smoke test |
| A2 3 页面均可打开 | ✅ | `tests/web/test_web.py::test_pages_return_html` |
| A3 非 localhost 来源 403 | ✅ | `test_non_local_request_rejected_with_403` |
| A4 启动 0.0.0.0 拒启 | ✅ | `test_assert_loopback_bind_rejects_external` |
| A5 Portfolio 历史日期渲染 | ✅ | `test_research_portfolio_renders_from_snapshot` |
| A6 无快照 404 | ✅ | `test_research_portfolio_404_when_missing` |
| A7 Factors 列表 | ✅ | `test_research_factors_list` |
| A8 Ops job_runs 表 | ✅ | `test_ops_job_runs_returns_list` |
| A9 Chat SSE 完整序列 | ✅ | `test_chat_message_post_sse_returns_done` |
| A10 daemon 未跑不崩页 | ✅ | `test_ops_health_with_no_daemon_does_not_crash` |
| A11 字段命名规范 | ✅ | `scheduler_events_24h_by_level` |
| A12 workers=1 | ✅ | `test_server_module_passes_workers_1` |
| A13 仅 chat POST | ✅ | `test_only_chat_endpoints_use_post` |
| A14 portfolio.name 来自 snapshot | ✅ | `test_research_portfolio_renders_from_snapshot` |
| B1 后端覆盖率 ≥ 80% | ✅ 91% | `pytest --cov=akq_agents.web` |
| B2 ruff 0 warnings | ✅ | `ruff check src/ tests/` |
| B4 无 React/Vite/npm | ✅ | `requirements.txt` 仅含 fastapi + uvicorn + jinja2 |

## 9. 与 P1-P4 / P6 接口承诺

详见 spec §附录 A / B。要点：
- P5 严格只读（除 chat_messages 由 P4 LLMOrchestrator 写）
- `LLMOrchestrator.run_chat_turn` 是 web chat 后端唯一入口（不另起 LLM 链路）
- 工具列表通过 `ToolRegistry.list_anthropic_specs()` 暴露；未来若需要在前端展示模型工具，已就绪
- P6 容器化时启动入口稳定（`akq_agents.web.server:start`）；如对外暴露需在 P6 加反向代理 + TLS
