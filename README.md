# AKQ Agents — 单机版 A 股量化研究系统

一个由 LLM 辅助、daemon 自动调度的 A 股因子挖掘与组合研究系统。**advisory only，不下单实盘**。

主要工作面在 Web 控制台（`http://127.0.0.1:8765/`）：每天打开一次，用 5 分钟看首屏结论条决定"是否要动仓 / 有没有需要审核的 AI 因子提议"即可。

## 3 分钟上手

```bash
# 1. 启动 web + daemon (默认命令)
./start.sh                     # 或 ./start.sh up

# 2. 首次使用需回填历史行情 (约 1-2 小时, 一次性)
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app \
    data bootstrap --lookback 250

# 3. 打开浏览器
./start.sh open                # 自动用默认浏览器打开控制台
```

启动后浏览器访问 `http://127.0.0.1:8765/` 会跳到 `/research` 研究面板。**如果系统检测到还没数据，页面顶部会自动显示引导横幅告诉你下一步做什么**——不用担心看到空页迷茫。

## 每天怎么用（Web 端 5 分钟流程）

打开 `/research`，从上到下扫一眼：

1. **顶部数据新鲜度条** —— 一秒确认今日行情/组合/清单是不是最新。
2. **KPI 总览** —— 6 张卡片：今日 BUY / 今日 SELL / 当日 NAV / 累计超额 / 持仓数 / Shadow 待 demote。NAV 与累计超额卡片右下角有 60 日 sparkline 趋势线（绿=上涨 / 红=下跌 / 灰=持平）。
3. **"今日待办"结论条** —— 系统把所有信号翻译成**一句话结论 + 直接跳转按钮**：
   - `⚠️ 今日需要调仓：3 买 / 2 卖，共 5 笔待执行` → 点"查看交易清单"
   - `☕ 今日无需调仓 — 组合稳定，可休息` → 点"净值曲线"或"Paper Trading"查看表现
   - `🔧 系统还没有产出今日结果` → 点跳转去 `/ops` 手动触发
4. **Tab 分组**（4 组，切到才加载）：交易 / 组合 / 因子 / LLM Lab —— 按需要展开对应卡片。
5. **右下角 ⌨ 快捷键浮标** —— 点开查看所有快捷键（`g+r/o/d/c/l` 跳转 / `r` 刷新本页 / `/` 聚焦搜索）。

日常两个高频动作：
- **调仓**：点 KPI 的"今日 BUY / SELL"跳到交易清单。清单按金额从大到小排，超过 15 条时自动分"Top 10 大额 + 剩余折叠"两段。逐条 ✓ 或"📦 全部执行"（弹窗会显示总买入/卖出金额和净流出，避免盲点）。
- **审核 AI 因子建议**：/research 页 → LLM Lab tab → "LLM 因子构建建议"卡片，✓ 接受 / ✗ 拒绝。

## 启动脚本速查

```bash
./start.sh          # 启动 web + daemon（默认，等价于 ./start.sh up）
./start.sh restart  # 重启 (改配置后使用)
./start.sh stop     # 停止
./start.sh status   # 看进程 + 关键健康指标
./start.sh logs     # tail web.log + daemon.log
./start.sh open     # 用默认浏览器打开 web 控制台
./start.sh help     # 完整帮助
```

## 系统架构

```
                        ┌─────────────────────────────────────────────┐
                        │              ./start.sh up                   │
                        └────────────┬─────────────┬───────────────────┘
                                     │             │
                          ┌──────────▼──┐    ┌─────▼──────────┐
                          │  web 进程    │    │  daemon 进程    │
                          │  (uvicorn)  │    │  (APScheduler) │
                          └──────┬──────┘    └─────┬──────────┘
                                 │                 │
                                 └─────┬───────────┘
                                       │ 共享
                          ┌────────────▼─────────────┐
                          │   data/meta.db (SQLite)  │
                          │  data/parquet/ohlcv      │
                          └──────────────────────────┘
```

- **web 进程** — FastAPI + Jinja，5 个页面（Research / Ops / Data / Chat / Logs）+ 各种 trigger endpoint
- **daemon 进程** — APScheduler 跑定时任务（数据刷新 / 盘后批处理 / 因子发现 / LLM brainstorm 等）
- **共享存储** — SQLite WAL 模式 + Parquet（按日分区）

两个进程通过 SQLite WAL + UNIQUE 约束协调（`(job_id, partition)` 防双写）。

## 数据流（盘后一天的真实链路）

```
16:00–21:30  data_refresh       窗口 cron（每 30 分钟一次）
       ↓                        → akshare 拉全 A 股 OHLCV
       ↓                        → 写 ohlcv parquet (date=YYYY-MM-DD)
       ↓                        → quality_gate 校验
                                → meta.db.refresh_state status=ok

16:30  batch_post_close          cron 触发 → workflow.run_once
       ├─ PortfolioAgent._run_p3
       │   ├─ get_universe(today) → ~5500 股票
       │   ├─ get_ohlcv_loose(...) → 历史 OHLCV
       │   ├─ FactorEngine.compute(ohlcv, registry.list_all())
       │   │     → 当前已注册 active 因子（默认 7 个预置 + N 个 accepted）
       │   ├─ Preprocessor (winsorize + zscore)
       │   ├─ CompositeScorer (IR-EWMA 加权)
       │   ├─ RiskFilter (新股/停牌/极价/低流动性)
       │   ├─ PortfolioOptimizer (top 50 + max_single=5%
       │   │                       + max_industry=30% + 行业中性)
       │   ├─ PortfolioSnapshotStore.write
       │   ├─ PortfolioBacktester.rebuild_full_history
       │   ├─ PaperTradingStore.freeze_today_cohort
       │   │                + update_track_perf
       │   └─ generate_trade_list → trade_list_cohorts (BUY/SELL/HOLD)
       └─ AnalystAgent (LLM 盘后总结，写 reports/*.md)

17:00  batch_deep_research       cron → 深度归因 / 长报告（每日）
16:35  board_refresh             cron → 抓当日行业板块快照（同花顺），历史随天累积
17:30  factor_promote_shadows    cron → 把符合门槛的 shadow 因子转 accepted
03:00  factor_eviction (周一)    cron → 周度淘汰衰减因子

每 120 分钟（交易日）factor_discovery
       → DSL 抽样 20 个候选 → IS 评估
       → 通过门槛 (IC≥0.015, IR≥0.30, |corr|≤0.7) 进 shadow
       → 已 shadow 因子复评 OOS：
           - 满 20-60 天且 |IR|≥0.15 → promote (accepted, register 进 registry)
           - 满 60 天且 |IR|<0.10  → demote (rejected)
           - 中间                  → 继续观察

每天 21:00 factor.code_brainstorm
       → LLM 看现状 (sandbox 能力圈 + 历史拒绝率 + 已上线因子) → 提议 10 个新 code recipe
       → 写 factor_proposals status='llm_suggested'
       → 等用户在 /research 页 ✓接受 / ✗拒绝

每 5 分钟  retry_fetch_errors / health_heartbeat

每 30 分钟 alerter
       → 巡检 3 条规则: NAV 单日异动 / data refresh 连续失败 / accepted 因子衰减
       → 触发时写 events.alert.* + macOS 系统通知（24h cooldown）
```

## 快速开始

### 推荐环境

```bash
conda env: akq310
python:    3.10.20
解释器:    /opt/anaconda3/envs/akq310/bin/python
依赖:      akshare 1.18.x, akquant 0.2.x, fastapi, apscheduler, pyarrow
```

详细启动 / 停止 / 状态命令见上方"启动脚本速查"。首次部署的数据回填命令见"3 分钟上手"。

## 核心能力

### 数据层（P1，已稳定）

- 全 A 股动态股票池（每日早 16:00 自动刷新）
- OHLCV Parquet 缓存按日分区，元数据 SQLite WAL
- akshare 限频/重试/质量门 + 交易日历感知
- 单股查询：`cli data inspect 600519`

### 因子体系

**3 个来源，1 个生命周期**：

| 来源 | 命名 | 怎么来的 |
|---|---|---|
| 预置 | `momentum_5/20/60`, `reversal_5`, `volatility_20`, `amount_20`, `log_amount_20` | 手工实现在 `services/factors/*.py`，7 个 |
| DSL 自动发现 | `auto_{op}_{base}_{window}_{direction}_{hash}` | daemon 每 120 分钟跑 `DiscoveryEngine`，从 `46 base × 37 op × 12 window × 2 direction` 抽样评估 |
| LLM 自由代码 | `code_{concept}_{hash6}` | 每天 21:00 `LLMCodeFactorBrainstormer` 让 LLM 直接写 Python compute 函数走 sandbox 编译，不限 DSL 空间 |

> **注**：历史 DSL 受限的 LLM 提议路径 (`factor.brainstorm`) 已下线（LLM 撞库率 100%）。现在 LLM 只走 `factor.code_brainstorm` 自由代码路径。

**生命周期**：`llm_suggested` (LLM 提议待审核) → `pending`/`shadow` (人工✓ 后进 OOS 观察) → `accepted` (注册进 registry，进入组合) / `demoted` (不达标)。

`/research` 页 → "LLM Lab" tab → "LLM 因子构建建议" 卡片可以逐条 ✓/✗。接受后 daemon 立刻并发跑 90 天 IS-IC backfill (最多 4 并发)，前端可实时看到 IC 曲线。

### 组合机（P3a）

- IR-EWMA 加权的 CompositeScorer（不是 equal weight）
- inverse-vol top 50
- max_single_weight=5%，max_industry_weight=30%（行业中性化）
- RiskFilter 硬过滤新股/停牌/极价/低流动性
- 换手抑制（turnover_aversion）
- PortfolioBacktester 重建 NAV 曲线 vs 沪深 300 benchmark

### Paper Trading（前向跟踪）

- 每日盘后 freeze 当日 cohort 建仓快照
- 每天用最新 close 估值（停牌票走 lookup 最近有效价 — 冷热路径对称）
- `paper_track_perf` 表记录每个 cohort 的当前 return / benchmark return / excess

### Trade List 闭环

- 每日盘后基于 weight diff 生成 BUY/SELL/HOLD 清单
- 用户在 web `/research` 页面"今日交易清单"卡片：
  - 单条 ✓ → mark executed + 同步 holdings
  - "📦 全部执行" 一键标记 → 同步 holdings 到 target

### 板块看板（`/board`）

独立页面，行业板块行情分析（数据源同花顺行业板块 —— 东财 `_em` 系列本地网络不可用）：

- **大盘云图** — Treemap，面积=成交额、颜色=涨跌幅（A 股红涨绿跌），一眼看板块强弱与权重
- **板块排行榜** — 涨跌幅/成交额/资金净流入/涨跌家数/领涨股，内嵌 diverging 涨跌条，表头点击排序
- **板块轮动热力图** — 近 N 日 × 波动最大 Top30 板块，色阶按分位数动态，看板块轮动
- **板块 K 线** — 点排行榜板块名或云图方块 → 弹出该板块日 K（candlestick + MA5/10/20）
- **历史回填** — 一键拉近 N 日**真实**板块历史（同花顺行业指数日线，~20 秒，后台线程 + 进度）
- **自动增量** — `board.refresh_daily` daemon job 每交易日 16:35 抓当日快照，历史随天累积

存储照抄数据层模式：parquet 按日 hive 分区（`data/parquet/board/`）+ meta.db `board_refresh_state` 表。
当前为**板块级单层**云图（个股级需个股→板块映射 + 流通市值，本地网络暂不可得）。

### LLM Agent

| Agent | 角色 |
|---|---|
| AnalystAgent | 盘后跑一次 LLM，写 markdown 报告（context 已经齐备 portfolio + attribution + data_health） |
| ChatAgent | `/chat` 页面对话，14 个只读 tool（`get_data_health` / `list_factors` / `get_portfolio_snapshot` / `get_today_trade_list` / `factor_postmortem` / `attribute_nav_drop` / ...） |
| LLMCodeFactorBrainstormer | 每天 21:00 让 LLM 直接写 Python compute 函数（走 sandbox 编译，不限 DSL 空间） |

LLM 网关：本地 Anthropic gateway `http://127.0.0.1:18931`（需另行启动）。

### 自动告警 (M17 alerter)

daemon 每 30 分钟巡检 3 条规则，触发时写 `events.alert.*` + 调 `osascript` 发 macOS 系统通知（24 小时 cooldown 防止刷屏）：

| 规则 | 阈值 | events kind |
|---|---|---|
| NAV 单日异动 | `\|daily_return_net\| > 15%` | `alert.nav.abnormal` (level=error) |
| 数据刷新连续失败 | `data.refresh_daily` 最近 2 次都 failed | `alert.data.refresh_failed` (error) |
| 因子衰减 | accepted/builtin 因子近 30 天平均 `\|IR\| < 0.05` | `alert.factor.decayed` (warning) |
| 因子指标陈旧 | factor_metrics 表 N 天无新写入 (M19) | `alert.factor.metrics_stale` (warning) |

阈值在 `config/scheduler.yaml` 里 `alerter.*` 字段可调。`/ops` 页 events 流可看完整历史。

### Web 控制台

6 个页面（`localhost:8765`，导航顺序按使用频率）：

| 路径 | 内容 |
|---|---|
| `/research` | **主工作面**：今日交易清单 + 真实持仓 + 今日组合 + **因子相关性热力图** + **今日异动诊断** + 因子表现 + 净值回测 + Paper Trading + 因子归因 + 自动发现流水 + **Shadow 战况看板** + LLM 因子建议审核。顶部有 KPI 总览 + "今日待办"结论条，4-tab 分组（交易 / 组合 / 因子 / LLM Lab） |
| `/board` | **板块看板**：大盘云图（Treemap）+ 板块排行榜（可排序）+ 轮动热力图 + 板块 K 线弹窗 + 历史回填 |
| `/ops` | 系统健康度、job_runs 历史、events 流、手动 trigger 按钮 |
| `/data` | AKShare 数据浏览器（17 个接口） |
| `/chat` | LLM 对话 + tool use（实时 SSE） |
| `/logs` | daemon / web 日志 tail |

**全局 UX 特性**：

- **顶部数据新鲜度条** — 每 30 秒轮询 `/api/ops/data-freshness`，一眼看今日行情 / 组合 / 清单 / 因子的最新数据日期
- **首次使用引导横幅** — 检测 OHLCV 数据缺失或组合未生成时自动显示指引，可 dismiss
- **导航健康红点** — 导航栏 "运维 Ops" 右侧的红/黄小圆点，daemon 挂了 / 今日批处理失败 / 数据异常时任意页面都能看见
- **右下角 ⌨ 快捷键浮标** — 常驻按钮，点击弹出快捷键面板
- **Ops 长任务进度链** — "立即跑一次完整流程"等 1-15 分钟任务，console 实时显示当前步骤（`✓ 拉数据 → ✓ 跑因子 → ▶ 生成组合…`）
- **Chat 常驻预设问题** — 对话中随时点开"💡 常问的问题"折叠区，8 个预设 chip 直接抛出
- **Logs 级别快捷 pill** — 三态互斥（全部 / 警告+ / 仅错误），前端二次过滤，找问题最快
- **快捷键**（也可按 `?` 或点浮标查看）：
  - `g+r/o/d/c/l` — 跳转 研究 / 运维 / 数据 / 对话 / 日志
  - `r` — 刷新本页数据
  - `/` — 聚焦搜索框
  - `Esc` — 关闭弹窗

## 当前限制

- **advisory only，不下单实盘**：`trade_list_cohorts` 每天生成建议，`holdings` 表手动校准（一键执行只是模拟）。
- **Paper trading 仅事后跟踪**：cohort 当日按 close 冻结建仓价，之后每日按 latest close 估值。
- **单机部署**：web/daemon 两进程通过 SQLite 同步，没有多用户/权限/SSO。
- **数据源限频**：akshare 默认 1 req/s，全量回填需 1-2 小时。

## 关键配置

**日常最常调**（散户根据本金 / 执行力）：

- `config/system.yaml` `portfolio:` 段 — 持仓数 (`top_n`) / 单票上限 / 单行业上限 / **换手抑制** (`turnover_aversion` 越小换手越低)
- `config/system.yaml` `trade_list:` 段 — 假定本金 / **最小可执行金额** (`min_trade_amount` 抬高砍碎单) / **最小权重变化阈值** (`min_weight_change`)

> 每字段都有详细中文注释 + 建议范围，直接编辑即可，改完 `./start.sh restart` 让 daemon 重新装载。

**系统级配置**（一般无需动）：

- `config/system.yaml` — universe / research / risk / backtest
- `config/scheduler.yaml` — daemon job 调度时间表 + alerter 阈值
- `config/llm.yaml` — LLM gateway / analyst / chat 配置
- `config/data.yaml` — akshare 限频 / 缓存策略
- `config/web.yaml` — web 端 polling 间隔

## 关键文件

- 启动脚本：`start.sh`
- 入口：`src/akq_agents/cli/app.py`（CLI）、`src/akq_agents/web/app.py`（FastAPI）
- 数据层：`src/akq_agents/services/data/`（含 `board_repository.py` 板块快照/历史）
- 因子：`src/akq_agents/services/factors/`（base / engine / discovery / llm_brainstorm / proposal_store）
- 组合：`src/akq_agents/services/portfolio/`（composite / optimizer / backtester / paper_trading / trade_list）
- LLM：`src/akq_agents/services/llm/`、`src/akq_agents/agents/analyst_agent.py`、`chat_agent.py`
- 调度：`src/akq_agents/orchestrator/`（scheduler / job_runner / jobs/，含 `jobs/board_refresh.py`）
- Web：`src/akq_agents/web/`（api/ + templates/，含板块看板 `api/board.py` + `templates/board.html.j2`）

## 辅助脚本（`scripts/`）

| 脚本 | 用途 |
|---|---|
| `run_once.py` | 等价于 `cli run-once`，手动跑一次盘后 workflow |
| `query_latest.py` | 快查最近一日组合/因子产物 |
| `analyze_research.py` | 离线研究分析脚本 |
| `backup.sh` | 备份 `data/`（sqlite + parquet） |
| `backfill_*.py` | 一次性回填工具：benchmark / factor_metrics_history / industry_map / paper_trading / portfolio_history / stock_names / universe_from_ohlcv |

## 常用 CLI 命令

```bash
PY=/opt/anaconda3/envs/akq310/bin/python
export PYTHONPATH=src

$PY -m akq_agents.cli.app doctor                     # 健康自检
$PY -m akq_agents.cli.app run-once                   # 手动跑一次盘后 workflow
$PY -m akq_agents.cli.app data bootstrap --lookback 250  # 首次回填
$PY -m akq_agents.cli.app data refresh               # 增量当日数据
$PY -m akq_agents.cli.app data status                # 看 refresh_state
$PY -m akq_agents.cli.app data inspect 600519        # 看单股缓存
$PY -m akq_agents.cli.app factors list               # 列因子
$PY -m akq_agents.cli.app factors inspect momentum_5 # 看因子历史
$PY -m akq_agents.cli.app factors discover           # 手动跑一轮自动发现
$PY -m akq_agents.cli.app factors proposals          # 查 LLM 提案
$PY -m akq_agents.cli.app portfolio explain --date 2026-06-23  # 解释当日组合
$PY -m akq_agents.cli.app trade-list                 # 看今日交易清单
$PY -m akq_agents.cli.app paper summary              # paper trading 总览
$PY -m akq_agents.cli.app holdings list              # 看真实持仓
$PY -m akq_agents.cli.app holdings set <code> <qty>  # 校准持仓
$PY -m akq_agents.cli.app daemon status              # daemon 状态
$PY -m akq_agents.cli.app daemon runs --last 20      # 任务历史
$PY -m akq_agents.cli.app daemon events --last 20    # 事件流
$PY -m akq_agents.cli.app llm calls --last 20        # LLM 调用历史
$PY -m akq_agents.cli.app llm sessions               # LLM session 列表
$PY -m akq_agents.cli.app chat                       # CLI 聊天 REPL
```

## 开发

```bash
PY=/opt/anaconda3/envs/akq310/bin/python

$PY -m pytest tests/ -q                              # 全量测试 (~290 用例)
$PY -m pytest tests/data/ --cov=akq_agents.services.data
$PY -m ruff check src/ tests/                        # lint
```

## 项目演进史

从 P1 → m1...m25 多轮迭代，关键里程碑：

| 阶段 | 内容 |
|---|---|
| P1 数据层 | akshare 接入 + WAL meta.db + 交易日历 + 质量门 |
| P2 调度守护 | APScheduler + JobRunner + self_heal + 优雅停机 |
| P3a 组合机 | 因子体系 + Composite + Optimizer + Backtester |
| P4 LLM Agent | AnalystAgent + ChatAgent + tool registry |
| P5 Web 控制台 | FastAPI + Jinja + ECharts + SSE chat |
| m7-m9 | NAV backtester + direction-flip + 行业中性化 |
| m11 | 执行轨迹透明化（job 详情 / 因子推理 / 实时日志） |
| m12 | Paper Trading 前向跟踪 + 交易清单 + 因子衰减预警 |
| m13 | oracle review 修 5 个真问题 |
| m14 | LLM 因子方向 brainstorm + 人工审核流 |
| m15 | 架构清理（删 7 个装饰品 agent + 老库 + NAV 真实性修复） |
| m16 | LLM 闭环（shadow 战况 + 归因诊断 + factor_postmortem + trade_list 闭环） |
| m17 | alerter 巡检（NAV 异动 / 数据失败 / 因子衰减 → macOS 系统通知） |
| m18 UX | 用户视角一致性打磨：顶栏品牌 / 主导航按频率排序 / ⌨ 快捷键浮标 / 首次使用引导横幅 / 结论条零操作日友好文案 / start.sh restart+open+help |
| m19 UX | 深挖高频动作：全部执行前显示总金额+净流出 / 大批量清单按金额排序+Top10+折叠 / Research 页 4-tab 懒加载 (首屏 API 请求 11→3) / KPI 卡片加 60 日 sparkline 趋势线 |
| m20 UX | 覆盖运维/对话/日志三页：Ops 长任务实时进度链 (job_steps → console) / Chat 常驻'💡 常问的问题'折叠区 / Logs 三态级别快捷 pill (全部/警告+/仅错误) |
| m19 | 修 H1+B2 lookahead bias（因子选股必须用 t-1 close）+ min_abs_ir 门槛统一 (builtin/auto/llm 同标准) |
| m23 | web → daemon 手动触发通道 picker（web 进程零 CPU 消耗） |
| m24 | user-facing job 结果流（`job_results` 表，前端异步取回） |
| m25 | LLM 自由 Python 代码因子（下线 DSL LLM 路径）+ picker fire-and-forget（消除告警 + 4 并发）+ 组合/交易清单参数配置化（`portfolio:` / `trade_list:` 段全中文注释）+ 前端 error_code/event kind i18n + trade list 空清单友好提示 + 快捷键 CSS 收官 + ruff 全绿 |
| board | 板块看板 `/board`：大盘云图（Treemap）+ 板块排行榜（可排序）+ 轮动热力图 + 板块 K 线弹窗 + 一键真实历史回填 + `board.refresh_daily` daemon 每日 16:35 自动抓（数据源同花顺行业板块） |

详细设计文档（部分已是历史档案，以代码为准）：`docs/superpowers/specs/`、`docs/superpowers/plans/`。
</content>
</invoke>