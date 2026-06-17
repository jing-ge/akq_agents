# P2 调度守护 — 设计文档

- 项目：akq-agents
- 阶段：P2（共 P1–P6 六阶段中的第二阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（数据层 DataRepository / TradingCalendar / RetryWorker / DataHealth）

---

## §1 目标与边界

### 目标

把现有"APScheduler 跑 `workflow.run_once` 一把梭"的最小调度，升级为可**长期 24/7 长跑**的盘中 + 盘后双轨调度守护：

- 盘中（09:30–15:00）：**轻量**地刷新行情快照、读最新 universe、计算实时因子快照、给前端推送增量。
- 盘后（15:30+）：**重型**地做 `data refresh` → 因子全量重算 → 回测 → 组合 → 日报。
- 崩溃恢复：进程被 kill / OOM / 机器重启后能从断点继续，不会丢任务、不会重复执行已完成任务。
- 调度状态对外可观测（`scheduler status`、events 表、Prometheus 指标占位）。

### 在做什么（P2 范围）

- 双轨调度器（盘中 tick + 盘后 batch），由 TradingCalendar 驱动。
- 任务定义统一为 `Job(id, schedule, fn, idempotency_key)`，幂等键写 sqlite。
- 崩溃恢复机制：每次任务前后写 `job_runs(id, status, started_at, finished_at)`，重启时扫表续跑。
- RetryWorker 接入调度器（P1 已实现，P2 注册成定时 job）。
- 任务级超时与并发上限，避免一个慢任务卡死调度线程。
- 信号处理（SIGTERM / SIGINT）：优雅停机，写入 stopped 状态。
- CLI：`akq-agents daemon start | stop | status | runs --last 20`。
- 配置：`config/scheduler.yaml` 新增。

### 不在做什么

- ❌ 分布式调度（多机 / Celery / 队列）— 单机 APScheduler 够用。
- ❌ Web 控制台（P5）。
- ❌ 告警通道实现（仅写 events 表，P6 告警系统消费）。
- ❌ 新因子 / 新组合算法（P3）。
- ❌ 容器化 / systemd unit 文件（P6）。
- ❌ 盘中分钟级行情拉取（数据层 P1 已声明不做分钟级）。

---

## §2 调度架构

### 整体拓扑

```
┌──────────────────────────────────────────────────────────────┐
│  QuantDaemon (单进程，foreground 或 nohup)                      │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SchedulerCore (APScheduler BackgroundScheduler)        │  │
│  │  ├─ tick_job        IntervalTrigger(60s, 09:30-15:00)  │  │
│  │  ├─ post_close_job  CronTrigger(15:30, trading days)   │  │
│  │  ├─ retry_job       IntervalTrigger(5m)                 │  │
│  │  ├─ health_job      IntervalTrigger(30s)                │  │
│  │  └─ self_heal_job   CronTrigger(每日 02:00)            │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  JobRunner（每个 Job 经此入口）                            │  │
│  │  - is_trading_day 护栏                                    │  │
│  │  - idempotency check (sqlite job_runs)                   │  │
│  │  - timeout enforcement (concurrent.futures + cancel)     │  │
│  │  - exception → events 表 + reason_code                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SignalHandler (SIGTERM/SIGINT → graceful shutdown)      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
                  data/meta.db（P1 已建库）
                  + 新增表：
                    - job_runs
                    - daemon_state
                    - events
```

### 关键设计点

1. **单进程多线程，不引入消息队列**：APScheduler 自带 `ThreadPoolExecutor`，最多 N 个并发 worker（配置默认 4），保持 YAGNI。
2. **TradingCalendar 强制护栏**：所有"盘中 / 盘后"job 进入 JobRunner 后第一件事就是 `repo.is_trading_day(today)`；非交易日直接 `skip(reason=NOT_TRADING_DAY)`。
3. **幂等键设计**：`idempotency_key = f"{job_id}:{partition}"`，partition 通常是 `YYYY-MM-DD`（盘后）或 `YYYY-MM-DD-HHMM`（盘中 tick）。`job_runs` 表 unique 索引；重复触发直接 noop。
4. **超时与撤销**：`JobRunner` 把每个任务塞进 `ThreadPoolExecutor.submit`，超过 `job.timeout_s`（配置）就标记 timeout、记 events、**不强杀**（Python 线程无法强杀，等下次重启），但状态会让后续不再重复触发同一 partition。
5. **崩溃恢复（self_heal）**：启动时扫 `job_runs` where status='running' AND started_at < now - 6h → 标记 `crashed`，配合调度策略决定下一次是否补跑（盘中 tick 不补，盘后 batch 一定补）。
6. **状态最小化**：所有持久状态写 sqlite `data/meta.db`，复用 P1 的 db；新增 3 张表（DDL 在 §3），不引入 Redis。
7. **事件流**：`events(ts, level, kind, payload_json)` 用于后续 P5 Web 控制台和 P6 告警系统消费。所有 job 开始/结束/失败/skip 都写 events。

### 时间窗（A 股，timezone Asia/Shanghai）

| Job | 触发 | 期望耗时 | 任务 |
|---|---|---|---|
| `tick.intraday` | 每分钟，09:30–11:30 + 13:00–15:00，仅交易日 | < 10s | 拉 `stock_zh_a_spot_em` 快照、计算实时因子快照、推送事件 `intraday_snapshot_ready` |
| `batch.post_close` | 15:30，仅交易日 | < 60min | `repo.refresh_daily(today)` → `FactorAgent` 重算 → `BacktestAgent` → `PortfolioAgent` → `ReportAgent` |
| `batch.deep_research` | 22:00，仅交易日 | < 90min | 滚动重训因子有效性、ResearchAgent、归因报告（P3 接入） |
| `retry.fetch_errors` | 每 5 分钟 | < 60s | `RetryWorker.run_once()` 消费 fetch_errors 未 resolved 的记录 |
| `health.heartbeat` | 每 30 秒 | < 1s | 写 `daemon_state.last_heartbeat`、监控 db / disk |
| `maintenance.self_heal` | 每日 02:00 | < 5min | 扫 `job_runs` crashed、补跑 missed post_close、清理 events > 30d |

### 配置示例（`config/scheduler.yaml`，新增）

```yaml
scheduler:
  timezone: "Asia/Shanghai"
  thread_pool_size: 4
  jobs:
    tick_intraday:
      enabled: true
      timeout_s: 30
    batch_post_close:
      enabled: true
      timeout_s: 3600
      hour: 15
      minute: 30
    batch_deep_research:
      enabled: false   # 等 P3
      timeout_s: 5400
      hour: 22
      minute: 0
    retry_fetch_errors:
      enabled: true
      interval_minutes: 5
      timeout_s: 60
    health_heartbeat:
      enabled: true
      interval_seconds: 30
    maintenance_self_heal:
      enabled: true
      hour: 2
      minute: 0
  retention:
    events_days: 30
    job_runs_days: 90
```

---

## §3 数据流与时序

### 流程 1：盘后 batch（核心闭环）

```
15:30 cron 触发
  → JobRunner.start(job_id="batch.post_close", partition="2026-06-17")
       - 检查 trading_day → 是
       - 检查 job_runs 是否已 completed → 否
       - 写 job_runs(status="running", started_at=now)
  → repo.refresh_daily(today)                  # P1 已实现
  → FactorAgent.run() / BacktestAgent / Portfolio / Advisor / Report   # 复用现有 workflow
  → JobRunner.complete(status="ok")
       - 写 job_runs(status="ok", finished_at=now, rows_processed=N)
       - 写 events(kind="batch.post_close.ok")
  失败路径：
    - DataNotReady → status="skipped", reason="data_not_ready"
    - 其他异常    → status="failed", reason_code="UNKNOWN", traceback 写 events
```

### 流程 2：盘中 tick（轻量）

```
每分钟 IntervalTrigger（09:30–15:00 之间通过窗口判断）
  → JobRunner.start(job_id="tick.intraday", partition="2026-06-17-1342")
       - 检查 trading_day + 当前时间在 [09:30, 11:30) ∪ [13:00, 15:00) → 是
       - partition 已 run → noop（同一分钟重复触发时幂等）
  → gateway.fetch_spot()  ← 仅 1 个 AKShare 请求
  → 计算"快照型"因子：成交额排名、涨跌幅 top N、当日成交量异常
  → 写 events(kind="intraday_snapshot_ready", payload=...)  ← 供 P5 Web 推送
  → 不写 parquet（避免盘中频繁小文件）
```

### 流程 3：崩溃恢复

```
daemon 启动
  → self_heal_on_boot()
       - 找 job_runs.status='running' → 强制改为 'crashed'，写 events
       - 找今日 partition=today 的 batch.post_close 是否完成
            - 已完成 → noop
            - 没跑过 + 当前时间 > 15:30 → 立即补跑一次
            - 没跑过 + 当前时间 < 15:30 → 等 cron 触发
  → SchedulerCore.start()
```

### 流程 4：优雅停机

```
SIGTERM / SIGINT
  → SignalHandler.handle()
       - scheduler.pause()              # 拒绝新任务
       - 等待运行中任务 ≤ shutdown_grace_s (默认 30s)
       - scheduler.shutdown(wait=False)
       - 把仍 running 的 job_runs 标记 'interrupted'
       - 写 daemon_state(status='stopped')
       - sys.exit(0)
```

### 关键表 DDL（新增到 P1 的 `data/meta.db`）

```sql
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL,           -- e.g. "batch.post_close"
  partition TEXT NOT NULL,        -- e.g. "2026-06-17"
  status TEXT NOT NULL,           -- pending|running|ok|failed|skipped|timeout|crashed|interrupted
  reason_code TEXT,               -- 失败/skip 原因
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  payload_json TEXT,              -- 任务自定义结果摘要
  UNIQUE(job_id, partition)
);

CREATE INDEX IF NOT EXISTS idx_job_runs_status_started ON job_runs(status, started_at);

CREATE TABLE IF NOT EXISTS daemon_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT NOT NULL,           -- starting|running|stopping|stopped
  pid INTEGER,
  started_at TEXT,
  last_heartbeat TEXT,
  version TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,            -- info|warning|error
  kind TEXT NOT NULL,             -- e.g. "batch.post_close.ok"
  source TEXT,                    -- job_id 或模块名
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
```

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── orchestrator/
│   ├── scheduler.py            ← 重写：QuantDaemon + SchedulerCore
│   ├── job_runner.py           ← 新增：JobRunner（幂等、超时、记账）
│   ├── jobs/                   ← 新增：每个 job 一个文件
│   │   ├── __init__.py
│   │   ├── tick_intraday.py
│   │   ├── batch_post_close.py
│   │   ├── batch_deep_research.py    # P3 接入前可空实现
│   │   ├── retry_fetch_errors.py
│   │   ├── health_heartbeat.py
│   │   └── maintenance_self_heal.py
│   ├── workflow.py             ← 不动；被 batch_post_close 调用
│   ├── signal_handler.py       ← 新增
│   └── state_store.py          ← 新增：job_runs / daemon_state / events 读写封装
├── models/
│   └── scheduler_config.py     ← 新增：SchedulerConfig (pydantic)
└── cli/
    └── app.py                  ← 新增子命令 daemon:
                                    - daemon start
                                    - daemon status
                                    - daemon stop  (向 pidfile PID 发 SIGTERM)
                                    - daemon runs --last N --job-id X
                                    - daemon events --last N --level warning+
```

### 核心接口

```python
class JobRunner:
    def __init__(self, state_store: SchedulerStateStore, calendar: TradingCalendar): ...
    def run(self, job_id: str, partition: str, fn: Callable, *,
            timeout_s: int, requires_trading_day: bool = True) -> JobRunResult: ...

class SchedulerStateStore:
    def upsert_job_run(self, ...) -> None: ...
    def get_job_run(self, job_id, partition) -> JobRun | None: ...
    def list_recent_runs(self, limit, job_id=None) -> list[JobRun]: ...
    def heartbeat(self) -> None: ...
    def get_daemon_state(self) -> DaemonState: ...
    def write_event(self, level, kind, source, payload: dict) -> None: ...
    def list_events(self, limit, level_min=None) -> list[Event]: ...

class QuantDaemon:
    def __init__(self, config: SchedulerConfig, services: dict): ...
    def start(self, foreground: bool = True) -> None: ...
    def stop(self) -> None: ...           # 写 pidfile + send SIGTERM
    def status(self) -> DaemonStatus: ...  # 读 daemon_state + 简单心跳判定
```

### Job 定义模式

每个 job 文件提供两件东西：

```python
# orchestrator/jobs/batch_post_close.py
JOB_ID = "batch.post_close"

def register(scheduler: BackgroundScheduler, runner: JobRunner, cfg: SchedulerConfig, services):
    job_cfg = cfg.scheduler.jobs.batch_post_close
    if not job_cfg.enabled:
        return
    def _run():
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)
    scheduler.add_job(_run, CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
                      id=JOB_ID, replace_existing=True, max_instances=1)

def _do(services) -> dict:
    services["workflow"].run_once()
    return {"summary": "..."}
```

### 关键边界

- ❌ 不实现强杀超时任务（Python 线程限制；记账 + 重启可解）。
- ❌ 不实现多机 / 分布式锁（单机假设）。
- ❌ JobRunner 不知道业务，只管"幂等 + 超时 + 记账 + 护栏"。
- ❌ events 表不做查询索引爆炸；最多按 ts/kind 加索引。

### 测试策略（`tests/orchestrator/`）

```
tests/orchestrator/
├── test_job_runner.py            # 幂等、超时、trading_day 护栏、异常→failed
├── test_state_store.py           # 表 DDL、并发 upsert、retention 清理
├── test_signal_handler.py        # SIGTERM → graceful，partial run 标 interrupted
├── test_self_heal.py             # crashed 识别 + 补跑判断
├── test_jobs_post_close.py       # 注入 mock workflow，验证一次完整闭环
└── test_daemon_status.py         # heartbeat 时间窗判定
```

目标覆盖率：**`orchestrator/` ≥ 80%**。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents daemon start` 启动后 `daemon status` 5 秒内返回 `running` | shell 双终端验证 |
| A2 | 非交易日（mock 周日）盘后 job 自动 skip | `job_runs.status='skipped' AND reason='NOT_TRADING_DAY'` |
| A3 | 同一交易日重复触发 `batch.post_close` 第二次为幂等 noop | `job_runs` 只有 1 条 `ok` |
| A4 | kill -9 daemon 再启动后，扫描 `job_runs.status='running'` 全部转 `crashed` | self_heal 集成测 |
| A5 | 盘后 batch 跑完写入：`runtime_state.yaml`、`reports/`、`meta.db.events(kind='batch.post_close.ok')` | 端到端跑一次 |
| A6 | SIGTERM 优雅停机 ≤ shutdown_grace_s | 信号注入测试 |
| A7 | 盘中 tick 任务在 11:30–13:00 午休时段自动跳过 | mock 时间测试 |
| A8 | retry job 能消费 `fetch_errors.resolved=0` 并更新到 1 | P1 RetryWorker 接入测 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/orchestrator/` 覆盖率 ≥ 80% | `pytest --cov=akq_agents.orchestrator` |
| B2 | `ruff check src/akq_agents/orchestrator/ tests/orchestrator/` 零警告 | CI/本地命令 |
| B3 | 所有 job 注册函数有 docstring，包含触发时机和幂等性说明 | review |
| B4 | `daemon` CLI 子命令均有 `--help` 文本与示例 | CLI 验证 |

### C. 性能 & 稳定性验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | 盘中 tick 任务 P95 ≤ 10s（单次 spot fetch + 因子快照） | events 表统计 |
| C2 | 盘后 batch 在样本 universe（≥ 4000 标的）上 ≤ 60min | 实测计时 |
| C3 | daemon 连续运行 ≥ 48h 无内存泄漏（RSS 增长 < 50MB） | `ps`/htop 长跑观测 |
| C4 | retry job 不与 batch / tick 抢锁（thread_pool=4 时同时 4 个 job 互不阻塞） | 并发压测 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/scheduler.md` 记录：架构图、job 列表、配置项、CLI 用法、故障排查 |
| D2 | `README.md` 加 `daemon start/status/stop` 三条命令 |
| D3 | 与 P1 接口承诺一一对应：`get_ohlcv` 幂等只读、`refresh_daily` 单日幂等、TradingCalendar 护栏 |

### 里程碑参考

- M2.1 骨架（1 天）：`SchedulerConfig`、`SchedulerStateStore`、表 DDL、`JobRunner` 框架。
- M2.2 JobRunner 完整化（1 天）：幂等、超时、护栏、events 写入；单测 ≥ 80%。
- M2.3 各 job 注册（1–2 天）：tick / post_close / retry / heartbeat / self_heal；端到端 mock 走通。
- M2.4 SignalHandler + 崩溃恢复（1 天）：self_heal_on_boot、SIGTERM 测试。
- M2.5 CLI + 文档（1 天）：`daemon` 子命令、scheduler.md、README 更新。
- M2.6 长跑稳定性（≥ 48h）：本地 / 远端开发机长跑验证。

**预估总工时：5–7 工作日。**

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| Python 线程无法强杀 | 慢任务卡死 worker pool | 增大 thread_pool 或拆 job；timeout 记账后下次重启释放 |
| sqlite 并发写竞争 | events 写入丢失 | WAL 模式 + 单写线程；P2 验收 C4 压测 |
| 时区误判（盘中窗口） | tick 漏跑 / 误跑 | timezone 强制配置；单测覆盖夏令时/跨日 |
| 长跑内存泄漏 | OOM | 周期性 maintenance 重启 daemon（P6 加 systemd 提供）|
| APScheduler 与 SIGTERM 兼容性 | 停机不优雅 | shutdown(wait=False) + 自实现 grace 等待 |

### 越界声明

- ❌ 多机调度、消息队列、容器化（P6）
- ❌ 告警通道（P6）
- ❌ Web 控制台（P5）
- ❌ 分钟级 / Tick 级行情拉取

---

## 附录 A：与 P1 接口的依赖契约

P2 严格依赖 P1 提供的以下契约（不变更，不绕过）：

1. `DataRepository.refresh_daily(d)` 幂等（P2 重复触发安全）。
2. `DataRepository.is_trading_day(d)` 准确（P2 用作硬护栏）。
3. `RetryWorker.run_once()` 单次调用幂等（P2 注册成 5 分钟 job）。
4. `meta.db.fetch_errors` 表结构稳定（P2 的 retry job 直接消费）。
5. `DataHealth` schema 稳定（P2 不修改；P5 渲染）。

## 附录 B：与后续阶段的接口承诺

P3/P4/P5 将依赖以下契约（P2 必须保证）：

1. `job_runs` 表结构稳定（P5 Web 控制台直接渲染任务历史）。
2. `events` 表结构稳定（P5 实时事件流、P6 告警系统消费）。
3. `daemon_state.last_heartbeat` 字段稳定（P5 daemon 在线判定）。
4. 任一 job 都可由 CLI 手动单次触发（P3/P4 复用同一 JobRunner，不重新发明）。
5. `JobRunner.run()` 是 P3/P4 新增 job 的唯一注册入口。
