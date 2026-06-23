# AKShare + akquant 智能 AI 量化 Agent 协作系统

这是一个可扩展的多 Agent 量化研究系统，现已具备可运行的真实研究环境。

## 推荐环境

- conda 环境：`akq310`
- Python：`3.10.20`
- 已安装：`akshare 1.18.64`、`akquant 0.2.45`

解释器路径：

```bash
/opt/anaconda3/envs/akq310/bin/python
```

## 常用命令

> 以下命令假设你已 `cd` 到项目根目录（含 `pyproject.toml` 的目录）。
> 使用 conda 环境 `akq310`，Python 路径见下方。

```bash
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app doctor
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app run-once
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app query --section all --limit 5
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app analyze
```

## 当前能力

- `AKShare` 真实数据路径已可用
- `akquant` 真实回测适配层已接入
- 多 Agent 工作流、日报、数据库、风控、CLI 全部可运行
- **P1 数据层**：全 A 股动态股票池 + Parquet 缓存 + 限频/重试/质量门 + 交易日历感知 + `meta.db` WAL
- **P2 调度守护**：盘后 batch + retry + heartbeat 长跑守护；启动期 self_heal + 优雅停机；任务幂等 + 事件流
- **P3a 多因子组合机**：7 因子注册表 + Preprocessor + CompositeScorer (equal) + Optimizer (inverse_vol top-N) + Attributor + 滚动 IC/IR Evaluator
- **P4 LLM Agent 层**：AnalystAgent (盘后离线) + ChatAgent (CLI REPL，4 个只读工具)；本地 Anthropic 网关 (`127.0.0.1:18931`)
- **P5 Web 控制台**：FastAPI + Jinja + HTMX + ECharts CDN；3 页（Ops / Research / Chat）；localhost-only；SSE 聊天

## 当前限制

- **advisory only，不下单实盘**：trade_list_cohorts 每天生成 BUY/SELL 建议，但 `holdings` 表需要在 web `/trading` 页手动校准。系统不接券商接口、不下单。
- **paper trading 仅事后跟踪**：cohort 当日按 close 冻结建仓价，之后每日按 latest close 估值，仅作为"如果当天按此组合开仓今天表现如何"的参考。
- **单机部署**：web/daemon 两进程通过 SQLite WAL 同步状态，没有多用户/权限/SSO。

## 关键文件

- 配置：`config/system.yaml`、`config/data.yaml`、`config/scheduler.yaml`、`config/llm.yaml`、`config/web.yaml`
- 数据层（P1）：`src/akq_agents/services/data/`、`docs/data_layer.md`
- 调度守护（P2）：`src/akq_agents/orchestrator/`、`docs/scheduler.md`
- 组合机（P3a）：`src/akq_agents/services/factors/`、`src/akq_agents/services/portfolio/`、`docs/portfolio.md`
- LLM Agent（P4）：`src/akq_agents/services/llm/`、`src/akq_agents/agents/analyst_agent.py`、`src/akq_agents/agents/chat_agent.py`、`docs/llm_agents.md`
- Web 控制台（P5）：`src/akq_agents/web/`、`docs/web_console.md`
- 回测适配：`src/akq_agents/services/backtest_service.py`
- 半实盘说明：`docs/semi_live.md`
- 设计文档：`docs/superpowers/specs/` （P1-P5 五份 spec）

## P1 数据层快速上手

```bash
# 健康检查（不联网）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data status

# 全量回填历史（联网；首跑 ~4-6 小时）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data bootstrap --lookback 250

# 增量刷新当日（联网；< 30 分钟）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data refresh

# 查看单股缓存
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data inspect 600519
```

详见 `docs/data_layer.md`。

## P2 调度守护快速上手

```bash
# 前台启动 daemon（Ctrl+C 触发优雅停机）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app daemon start

# 查看 daemon 状态（无须 daemon 在跑也能读 daemon_state.json）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app daemon status

# 查看最近 20 个任务运行
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app daemon runs --last 20

# 查看最近事件
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app daemon events --last 20
```

详见 `docs/scheduler.md`。

## P3a 组合机快速上手

```bash
# 列出所有因子 + 最近一次 metrics（首次跑会全为 null）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app factors list

# 看某因子历史 metrics
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app factors inspect momentum_5

# 看某日组合快照
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app portfolio explain --date 2026-06-17
```

详见 `docs/portfolio.md`。

## P4 LLM Agent 快速上手

```bash
# 启动 ChatAgent REPL（需 LLM 网关 http://127.0.0.1:18931 在跑）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app chat

# 看最近 LLM 调用
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app llm calls --last 20

# 看 chat sessions
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app llm sessions
```

详见 `docs/llm_agents.md`。

## P5 Web 控制台快速上手

```bash
# 前台启动 web（Ctrl+C 退出；localhost-only）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app web start

# 浏览器访问
open http://127.0.0.1:8765/
```

3 个页面：Ops（系统状态 + 任务历史 + 事件流）/ Research（组合 + 因子有效性）/ Chat（LLM 对话）。

详见 `docs/web_console.md`。

## 开发命令

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/data/ --cov=akq_agents.services.data
/opt/anaconda3/envs/akq310/bin/python -m ruff check src/ tests/
```
