# P2 调度守护 — 设计文档

- 项目：akq-agents
- 阶段：P2（共 P1–P6 六阶段中的第二阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（数据层 DataRepository / TradingCalendar / RetryWorker / DataHealth）

---

## §1 目标与边界

### 目标

把现有"APScheduler 跑 `workflow.run_once` 一把梭"的最小调度，升级为可**长期 24/7 长跑**的盘后调度守护：

- 盘后（15:30+）：完成 `data refresh` → 因子全量重算 → 回测 → 组合 → 日报。
- 失败自愈：失败任务定时重试；崩溃恢复在**启动时**做一次扫描，把跑挂的任务标记并按规则补跑。
- 调度状态对外可观测（`daemon status`、events 表）。

> **v2 收敛说明**（oracle review 后）：原 v1 设计了盘中 `tick.intraday` 每分钟 job + `health.heartbeat` 30 秒一跳 + `maintenance.self_heal` 每日 02:00。v2 砍掉盘中 tick（YAGNI，无下游消费者；P5 dashboard 需要时直接 on-demand 调 `gateway.fetch_spot`），heartbeat 改 5 分钟，self_heal 仅在启动时跑（每日定时那次是为对称而对称）。

### 在做什么（P2 范围）

- 单轨调度器（盘后 batch + retry + 启动自愈），由 TradingCalendar 驱动。
- 任务定义统一为 `Job(id, schedule, fn)`，幂等键 `(job_id, partition)` 写 sqlite `job_runs` 唯一索引。
- 崩溃恢复机制：每次任务前后写 `job_runs(status, started_at, finished_at)`，**daemon 启动时**扫表把 `running/interrupted` 转 `crashed`，按规则补跑。
- RetryWorker 接入调度器（P1 已实现，P2 注册成 5 分钟定时 job）。
- 任务级超时（记账，不强杀）与并发上限。
- 信号处理（SIGTERM / SIGINT）：优雅停机，未完成任务标 `interrupted`。
- CLI：`akq-agents daemon start | status | runs --last 20 | events --last 20`。
- 配置：`config/scheduler.yaml` 新增。

### 不在做什么

- ❌ 分布式调度（多机 / Celery / 队列）— 单机 APScheduler 够用。
- ❌ 盘中实时调度 / tick job — YAGNI；如未来 P5 dashboard 需要盘中行情，由 API 端点 on-demand 拉取，不放调度器。
- ❌ Web 控制台（P5）。
- ❌ 告警通道实现（仅写 events 表，P6 告警系统消费）。
- ❌ 新因子 / 新组合算法（P3）。
- ❌ 容器化 / systemd unit 文件（P6）。
- ❌ `daemon stop` / `daemon status` 单独的 CLI 信号通讯（用 Ctrl+C 停；status 由读 daemon_state.json + 心跳判定）。

---

## §2 调度架构

### 整体拓扑

```
┌──────────────────────────────────────────────────────────────┐
│  QuantDaemon (单进程，foreground 或 nohup)                      │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SchedulerCore (APScheduler BackgroundScheduler)        │  │
│  │  ├─ batch.post_close   CronTrigger(15:30, 交易日)      │  │
│  │  ├─ batch.deep_research CronTrigger(周日 22:00)         │  │
│  │  ├─ retry.fetch_errors IntervalTrigger(5m)              │  │
│  │  └─ health.heartbeat   IntervalTrigger(5m)              │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  JobRunner（每个 Job 经此入口）                            │  │
│  │  - 启动期 self_heal_on_boot：扫 running/interrupted →    │  │
│  │    crashed，补跑规则按 job_id 白名单                       │  │
│  │  - is_trading_day 护栏（由 JobRunner 内部按 job_id 白名单）│  │
│  │  - idempotency check (sqlite job_runs)                   │  │
│  │  - timeout enforcement (记账，不强杀)                     │  │
│  │  - 异常 → events 表 + reason_code                         │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SignalHandler (SIGTERM/SIGINT → graceful shutdown)      │  │
│  │  - 未完成任务标 'interrupted'                              │  │
│  │  - 写 data/daemon_state.json (status='stopped')           │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
                  data/meta.db（P1 已建库，P1 附录 B 承诺 WAL）
                  + 新增表：
                    - job_runs
                    - events
                  data/daemon_state.json（单行状态用文件，不入 db）
```

### 关键设计点

1. **单进程多线程，不引入消息队列**：APScheduler 自带 `ThreadPoolExecutor`，最多 N 个并发 worker（配置默认 4），保持 YAGNI。
2. **TradingCalendar 强制护栏**：所有 batch.* job 进入 JobRunner 后第一件事就是 `repo.is_trading_day(today)`；非交易日直接 `skip(reason=NOT_TRADING_DAY)`。`retry.*` / `health.*` 在 JobRunner 内部 **白名单**绕过此护栏（不需要交易日）。
3. **幂等键设计**：`idempotency_key = f"{job_id}:{partition}"`，partition 通常是 `YYYY-MM-DD`（盘后）或 `YYYY-MM-DDTHH:MM`（retry/heartbeat 按时间窗）。`job_runs` 表 unique 索引；重复触发直接 noop。
4. **超时与撤销**：`JobRunner` 把每个任务塞进 `ThreadPoolExecutor.submit`，超过 `job.timeout_s`（配置）就标记 timeout、记 events、**不强杀**（Python 线程无法强杀，等下次重启），但状态会让后续不再重复触发同一 partition。
5. **崩溃恢复仅在启动时跑**：`self_heal_on_boot()` 扫 `job_runs.status IN ('running','interrupted') AND started_at < now - 6h` → 全部标 `crashed`，写 events；之后再判断今日 `batch.post_close` 是否需要补跑（白名单仅 `batch.post_close`、`batch.deep_research`；`retry` / `heartbeat` 不补，错过就错过）。
6. **APScheduler `misfire_grace_time=None`**：禁用 APScheduler 内置的 misfire 自动补跑；missed batch 全部由 `self_heal_on_boot` 接管。
7. **`daemon_state` 用文件不入 db**：单行状态没必要建表；写 `data/daemon_state.json`，少一张表少一次 join，启动/停机时整文件原子替换。
8. **事件流**：`events(ts, level, kind, source, payload_json)` 用于后续 P5 Web 控制台和 P6 告警系统消费。所有 job 开始/结束/失败/skip 都写 events。**events 写入失败 fallback 到 stderr 日志**，不影响 job 主流程。
9. **events.kind 命名规范**：见附录 C；所有 4 阶段统一遵守。

### 时间窗（A 股，timezone Asia/Shanghai）

| Job | 触发 | 期望耗时 | trading_day 护栏 | 任务 |
|---|---|---|---|---|
| `batch.post_close` | 15:30，仅交易日 | < 90min | ✓ | `repo.refresh_daily(today)` → P3 组合 pipeline → `AnalystAgent` (P4) |
| `batch.deep_research` | 周日 22:00 | < 90min | ✓（跑前判定本周是否有交易日） | 滚动重算 factor_metrics（P3）；P2 占位 + 空实现，P3 接入 |
| `retry.fetch_errors` | 每 5 分钟 | < 60s | ✗（白名单绕过） | `RetryWorker.run_once()` 消费 fetch_errors 未 resolved 的记录 |
| `health.heartbeat` | 每 5 分钟 | < 1s | ✗（白名单绕过） | 更新 `data/daemon_state.json` 的 `last_heartbeat` |

> **耗时承诺统一**：`batch.post_close` 含 P3 portfolio pipeline + P4 AnalystAgent，统一承诺 **< 90 分钟**（v1 写的 60min 是 P2 自身耗时，但 P3 spec 已说要到 90min，这里以 90min 为准）。

### 配置示例（`config/scheduler.yaml`，新增）

```yaml
scheduler:
  timezone: "Asia/Shanghai"
  thread_pool_size: 4
  jobs:
    batch_post_close:
      enabled: true
      timeout_s: 5400        # 90min
      hour: 15
      minute: 30
    batch_deep_research:
      enabled: false         # 等 P3 接入再 true
      timeout_s: 5400
      day_of_week: "sun"
      hour: 22
      minute: 0
    retry_fetch_errors:
      enabled: true
      interval_minutes: 5
      timeout_s: 60
    health_heartbeat:
      enabled: true
      interval_minutes: 5
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
       - 检查 job_runs 是否已 ok → 否
       - 写 job_runs(status="running", started_at=now)
  → repo.refresh_daily(today)                  # P1 已实现
  → P3 portfolio pipeline (FactorEngine → Preprocessor → Composite → Optimizer → Attributor)
  → P4 AnalystAgent.run() （失败 degrade 不阻断）
  → ReportAgent / 持久化 runtime_state.yaml / sqlite
  → JobRunner.complete(status="ok")
       - 写 job_runs(status="ok", finished_at=now, duration_ms=N, payload_json={...})
       - 写 events(kind="batch.post_close.completed", level="info", source="batch.post_close")
  失败路径：
    - DataNotReady → status="skipped", reason_code="DATA_NOT_READY"，events kind="batch.post_close.skipped"
    - 超时         → status="timeout"，events kind="batch.post_close.timeout"
    - 其他异常     → status="failed", reason_code="UNKNOWN", traceback 写 events kind="batch.post_close.failed"
```

### 流程 2：retry / heartbeat（无 trading_day 护栏）

```
retry.fetch_errors  每 5 分钟 IntervalTrigger
  → JobRunner.start(job_id="retry.fetch_errors", partition="<ISO 时间窗起点>", requires_trading_day=False)
  → RetryWorker.run_once()  ← P1 已实现
  → 写 events(kind="retry.fetch_errors.completed", payload={"resolved": N})

health.heartbeat  每 5 分钟 IntervalTrigger
  → 直接更新 data/daemon_state.json（不经 JobRunner，避免 job_runs 表灌水）
       {"status": "running", "pid": ..., "started_at": ..., "last_heartbeat": now, "version": "..."}
```

### 流程 3：崩溃恢复（仅启动时）

```
daemon 启动
  → self_heal_on_boot()
       a. 写 daemon_state.json {status="starting", pid=os.getpid(), started_at=now}
       b. 找 job_runs.status IN ('running', 'interrupted')
            - started_at < now - 6h → 标 'crashed'，写 events(kind="<job_id>.crashed")
       c. 补跑判定（仅白名单 job_id 走补跑）：
            - 白名单：batch.post_close, batch.deep_research
            - 找今日 partition=today 的 batch.post_close（status='ok'）是否存在
                 - 存在 → noop
                 - 不存在 + 当前时间 > 15:30 + 是交易日 → 立即触发一次（同步进 JobRunner）
                 - 不存在 + 当前时间 < 15:30 → 等 cron 触发
       d. 一次性 retention 清理：DELETE FROM events WHERE ts < now - 30d；
                                  DELETE FROM job_runs WHERE finished_at < now - 90d AND status != 'running'
  → SchedulerCore.start()
  → 写 daemon_state.json {status="running", ...}
```

### 流程 4：优雅停机

```
SIGTERM / SIGINT
  → SignalHandler.handle()
       - scheduler.pause()              # 拒绝新任务
       - 等待运行中任务 ≤ shutdown_grace_s (默认 30s)
       - scheduler.shutdown(wait=False)
       - 把仍 running 的 job_runs 标记 'interrupted'
       - 写 daemon_state.json(status="stopped", last_heartbeat=now)
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

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,            -- info|warning|error
  kind TEXT NOT NULL,             -- 命名规范见附录 C
  source TEXT,                    -- job_id 或模块名
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
```

`data/daemon_state.json` 文件 schema（**非表，原子替换写**）：

```json
{
  "status": "starting|running|stopping|stopped",
  "pid": 12345,
  "started_at": "2026-06-17T15:30:00+08:00",
  "last_heartbeat": "2026-06-17T16:00:00+08:00",
  "version": "akq-agents 0.2.0"
}
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
│   │   ├── batch_post_close.py
│   │   ├── batch_deep_research.py    # P3 接入前空实现：return {"status": "noop"}
│   │   ├── retry_fetch_errors.py
│   │   └── health_heartbeat.py       # 不经 JobRunner，直接更新 daemon_state.json
│   ├── workflow.py             ← 不动；被 batch_post_close 调用
│   ├── signal_handler.py       ← 新增
│   ├── state_store.py          ← 新增：job_runs / events 读写封装
│   └── daemon_state_file.py    ← 新增：data/daemon_state.json 原子读写
├── models/
│   └── scheduler_config.py     ← 新增：SchedulerConfig (pydantic)
└── cli/
    └── app.py                  ← 新增子命令 daemon:
                                    - daemon start                     (前台启动)
                                    - daemon status                    (读 daemon_state.json + 心跳判定)
                                    - daemon runs --last N [--job-id X]
                                    - daemon events --last N [--level warning]
```

### 核心接口

```python
class JobRunner:
    def __init__(self, state_store: SchedulerStateStore, calendar: TradingCalendar,
                 trading_day_required_jobs: set[str]):
        """trading_day_required_jobs: 需要 trading_day 护栏的 job_id 白名单"""

    def run(self, job_id: str, partition: str, fn: Callable, *,
            timeout_s: int) -> JobRunResult:
        """统一执行入口；护栏由 job_id 是否在白名单决定，不再每个 job 传参"""

class SchedulerStateStore:
    """只管 job_runs / events 两张表的读写。"""
    def upsert_job_run(self, ...) -> None: ...
    def get_job_run(self, job_id, partition) -> JobRun | None: ...
    def list_recent_runs(self, limit, job_id=None) -> list[JobRun]: ...
    def write_event(self, level, kind, source, payload: dict) -> None:
        """写失败 fallback 到 stderr，不抛异常"""
    def list_events(self, limit, level_min=None, since=None) -> list[Event]: ...
    def cleanup(self, events_keep_days: int, job_runs_keep_days: int) -> dict: ...

class DaemonStateFile:
    """data/daemon_state.json 的原子读写封装。"""
    def write(self, state: DaemonState) -> None: ...
    def read(self) -> DaemonState | None: ...
    def update_heartbeat(self) -> None: ...
    def is_alive(self, max_age_s: int = 600) -> bool:
        """读 last_heartbeat，超过阈值视为死亡（默认 10min = 2 个 heartbeat 周期）"""

class QuantDaemon:
    def __init__(self, config: SchedulerConfig, services: dict): ...
    def start(self) -> None:
        """前台启动；调 self_heal_on_boot → register_jobs → scheduler.start (blocking)"""
    def status(self) -> DaemonStatus:
        """读 daemon_state.json + is_alive 判定；不需要进程间 IPC"""
```

### Job 定义模式

每个 job 文件提供一个 `register` 函数：

```python
# orchestrator/jobs/batch_post_close.py
JOB_ID = "batch.post_close"

def register(scheduler: BackgroundScheduler, runner: JobRunner, cfg: SchedulerConfig, services):
    """每个交易日 15:30 触发；同日重复触发幂等。"""
    job_cfg = cfg.scheduler.jobs.batch_post_close
    if not job_cfg.enabled:
        return
    def _run():
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)
    scheduler.add_job(
        _run,
        CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,   # 禁用 APScheduler 内置 misfire；missed 由 self_heal 处理
    )

def _do(services) -> dict:
    services["workflow"].run_once()
    return {"summary": "..."}
```

### 关键边界

- ❌ 不实现强杀超时任务（Python 线程限制；记账 + 重启可解）。
- ❌ 不实现多机 / 分布式锁（单机假设）。
- ❌ JobRunner 不知道业务，只管"幂等 + 超时 + 记账 + 护栏"。
- ❌ 不再每日 02:00 定时 self_heal；只在启动时跑一次（含 retention 清理）。
- ❌ `daemon stop` 不做（Ctrl+C 即可）；CLI 只暴露 start / status / runs / events。
- ❌ events 表不做查询索引爆炸；最多按 ts / (kind, ts) 加索引。
- ❌ `health.heartbeat` 不写 events、不写 job_runs，仅更新文件（避免表灌水）。

### 测试策略（`tests/orchestrator/`）

```
tests/orchestrator/
├── test_job_runner.py            # 幂等、超时记账、trading_day 白名单、异常→failed、events 写入失败 fallback
├── test_state_store.py           # 表 DDL、并发 upsert、retention 清理、events 写失败 fallback 到 stderr
├── test_daemon_state_file.py     # 原子写、is_alive 判定
├── test_signal_handler.py        # SIGTERM → graceful，partial run 标 interrupted
├── test_self_heal.py             # crashed 识别 + 补跑判断（含 interrupted 也要扫到）
├── test_jobs_post_close.py       # 注入 mock workflow，验证一次完整闭环
└── test_daemon_status.py         # is_alive 时间窗判定（heartbeat 超过 2 周期视为死亡）
```

目标覆盖率：**`orchestrator/` ≥ 80%**。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents daemon start` 启动后 `daemon status` 5 秒内返回 `running` + 最近 heartbeat 不超过 10min | shell 双终端验证 |
| A2 | 非交易日（mock 周日）`batch.post_close` 自动 skip | `job_runs.status='skipped' AND reason_code='NOT_TRADING_DAY'` |
| A3 | 同一交易日重复触发 `batch.post_close` 第二次为幂等 noop | `job_runs` 只有 1 条 status='ok' |
| A4 | kill -9 daemon 再启动后，残留 `running` / `interrupted` 全部转 `crashed` | self_heal 集成测 |
| A5 | 盘后 batch 跑完写入：`runtime_state.yaml`、`reports/`、`meta.db.events(kind='batch.post_close.completed')` | 端到端跑一次 |
| A6 | SIGTERM 优雅停机 ≤ shutdown_grace_s，未完成任务标 `interrupted` | 信号注入测试 |
| A7 | `retry.fetch_errors` 在非交易日仍正常跑（白名单绕过护栏） | mock 周日跑测试 |
| A8 | retry job 能消费 `fetch_errors.resolved=0` 并更新到 1 | P1 RetryWorker 接入测 |
| A9 | events 表写入故意失败（mock locked db）→ stderr 有日志、job 主流程不挂 | 单测 |
| A10 | `heartbeat` 任务不在 `job_runs` 表灌数据（仅更新文件） | 长跑 10 分钟后查表 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/orchestrator/` 覆盖率 ≥ 80% | `pytest --cov=akq_agents.orchestrator` |
| B2 | `ruff check src/akq_agents/orchestrator/ tests/orchestrator/` 零警告 | CI/本地命令 |
| B3 | 所有 job 注册函数有 docstring，包含触发时机和幂等性说明 | review |
| B4 | `daemon` CLI 子命令均有 `--help` 文本与示例 | CLI 验证 |
| B5 | events.kind 全部符合附录 C 命名规范 | grep + 单测枚举校验 |

### C. 性能 & 稳定性验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | 盘后 batch 在样本 universe（≥ 4000 标的）上 ≤ **90min**（含 P3 portfolio + P4 AnalystAgent） | 实测计时 |
| C2 | daemon 连续运行 ≥ 48h 无内存泄漏（RSS 增长 < 50MB） | `ps`/htop 长跑观测 |
| C3 | retry job 不与 batch 抢锁（thread_pool=4 时并发不阻塞） | 并发压测 |
| C4 | `meta.db` WAL 模式生效（`PRAGMA journal_mode` 返回 'wal'） | 启动期断言 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/scheduler.md` 记录：架构图、job 列表、配置项、CLI 用法、故障排查、events.kind 规范 |
| D2 | `README.md` 加 `daemon start/status/runs/events` 命令 |
| D3 | 与 P1 接口承诺一一对应：`refresh_daily` 单日幂等、TradingCalendar 护栏、WAL 模式 |

### 里程碑参考

- M2.1 骨架（1 天）：`SchedulerConfig`、`SchedulerStateStore`、表 DDL、`DaemonStateFile`、`JobRunner` 框架。
- M2.2 JobRunner 完整化（0.5 天）：幂等、超时、白名单护栏、events 写入；单测 ≥ 80%。
- M2.3 各 job 注册（1 天）：post_close / deep_research(空) / retry / heartbeat；端到端 mock 走通。
- M2.4 SignalHandler + 启动期 self_heal（0.5 天）：crashed/interrupted 识别 + 补跑 + retention 清理。
- M2.5 CLI + 文档（0.5–1 天）：`daemon` 子命令、scheduler.md、README 更新。
- M2.6 长跑稳定性（≥ 24h；不要求 48h）：本地长跑验证 + heartbeat / events / WAL 自检。

**预估总工时：3–4 工作日**（v1 写的 5–7 天因砍 tick / heartbeat 表 / maintenance / stop CLI 而缩短）。

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| Python 线程无法强杀 | 慢任务卡死 worker pool | 增大 thread_pool 或拆 job；timeout 记账后下次重启释放 |
| sqlite 并发写竞争 | events 写入丢失 | P1 已承诺 WAL + busy_timeout=5000；write_event 写失败 fallback stderr |
| 时区配置错 | batch 错过 / 误触发 | timezone 强制配置；启动期断言 `Asia/Shanghai` |
| 长跑内存泄漏 | OOM | P6 加 systemd 提供守护；P2 仅做 ≥ 24h 自检 |
| APScheduler 与 SIGTERM 兼容性 | 停机不优雅 | shutdown(wait=False) + 自实现 grace 等待 |
| events.kind 飘移 | P5/P6 消费链路出错 | 附录 C 命名规范 + 单测枚举校验 |
| 节假日临时调整收盘时间 | batch 触发偏移 | 完全信任 TradingCalendar；P2 不动态调整 cron |

### 越界声明

- ❌ 多机调度、消息队列、容器化（P6）
- ❌ 告警通道（P6）
- ❌ Web 控制台（P5）
- ❌ 盘中 tick / 实时分钟级行情
- ❌ `daemon stop` 单独 CLI（Ctrl+C 即可）

---

## 附录 A：与 P1 接口的依赖契约

P2 严格依赖 P1 提供的以下契约（不变更，不绕过）：

1. `DataRepository.refresh_daily(d)` 幂等（P2 重复触发安全）。
2. `DataRepository.is_trading_day(d)` 准确（P2 用作硬护栏）。
3. `RetryWorker.run_once()` 单次调用幂等（P2 注册成 5 分钟 job）。
4. `meta.db.fetch_errors` 表结构稳定（P2 的 retry job 直接消费）。
5. `DataHealth` schema 稳定（P2 不修改；P5 渲染）。
6. **`meta.db` 启用 WAL + busy_timeout=5000**（P1 附录 B §6）。

## 附录 B：与后续阶段的接口承诺

P3/P4/P5 将依赖以下契约（P2 必须保证）：

1. `job_runs` 表结构稳定（P5 Web 控制台直接渲染任务历史）。
2. `events` 表结构稳定（P5 实时事件流、P6 告警系统消费）。
3. `data/daemon_state.json` 字段稳定（P5 daemon 在线判定）。
4. 任一 job 都可由 CLI 手动单次触发（P3/P4 复用同一 JobRunner，不重新发明）。
5. `JobRunner.run()` 是 P3/P4 新增 job 的唯一注册入口。
6. **events.kind 命名规范见附录 C**；所有阶段写 events 都遵守。

## 附录 C：events.kind 命名规范（跨 P2–P5 强制约束）

格式：`<domain>.<noun>.<verb_past>`

- **domain**（顶层域，固定集合）：
  - `batch` — 盘后/周期性大任务（P2 注册的 batch.* job）
  - `retry` — RetryWorker 相关（P2）
  - `data` — 数据层操作（P1，可选写）
  - `factor` — 因子相关（P3）
  - `portfolio` — 组合相关（P3）
  - `analyst` — 分析师 Agent（P4）
  - `chat` — 对话 Agent（P4）
  - `llm` — LLM 网关 / 工具调用（P4）
  - `daemon` — daemon 生命周期（P2 启停崩溃恢复）
- **noun**：被操作的对象，snake_case 单数。
- **verb_past**：动词过去分词或形容词状态：
  - `completed`（正常结束）
  - `failed`（异常结束）
  - `skipped`（条件不满足）
  - `timeout`（超时）
  - `crashed`（崩溃恢复时发现）
  - `interrupted`（优雅停机中断）
  - `started`（仅长任务初始化）
  - `deactivated` / `activated`（状态变更）
  - `blocked`（安全拦截）

### 标准化 kind 清单（v2）

| kind | 由谁写 | level | 含义 |
|---|---|---|---|
| `batch.post_close.completed` | P2 JobRunner | info | 盘后 batch 成功 |
| `batch.post_close.failed` | P2 JobRunner | error | 盘后 batch 异常 |
| `batch.post_close.skipped` | P2 JobRunner | info | 非交易日 / 数据未就绪 |
| `batch.post_close.timeout` | P2 JobRunner | warning | 超时记账 |
| `batch.post_close.crashed` | P2 self_heal | error | 启动时发现残留 running |
| `batch.post_close.interrupted` | P2 SignalHandler | warning | SIGTERM 中断 |
| `batch.deep_research.completed/failed/skipped/...` | P2/P3 | - | 同上规则 |
| `retry.fetch_errors.completed` | P2 retry job | info | RetryWorker 一轮完成（payload.resolved=N） |
| `data.refresh.completed/failed` | P1 RetryWorker / Repository（可选写） | info/error | 数据刷新结果 |
| `factor.metric.deactivated` | P3 | warning | 因子 IR 退化被禁用 |
| `factor.metric.activated` | P3 | info | 因子 IR 回升被启用 |
| `factor.metric.bootstrap` | P3 | warning | factor_metrics 表为空，CompositeScorer 退化为 equal weight |
| `factor.metric.evaluated` | P3 | info | deep_research 跑完一轮 metrics 更新 |
| `factor.data.missing` | P3 | warning | 因子计算缺数据 |
| `portfolio.snapshot.generated` | P3 | info | 当日组合生成完成（payload 含 n / turnover / 顶层因子贡献） |
| `portfolio.optimizer.fallback` | P3 | warning | MV 求解失败退化到 inverse_vol（P3b 起出现） |
| `analyst.brief.generated` | P4 | info | AnalystAgent 写出 markdown |
| `analyst.brief.degraded` | P4 | warning | LLM 不可用退化到模板 |
| `chat.session.created` | P4 | info | 新会话 |
| `llm.tool.failed` | P4 | warning | 工具执行异常 |
| `llm.tool.unknown` | P4 | warning | LLM 调了未注册工具 |
| `daemon.started` | P2 | info | daemon 启动完成 |
| `daemon.stopped` | P2 | info | daemon 优雅停机完成 |

**校验**：P2 实现期提供一个 `events.kind` 的 `Literal[...]` 类型或枚举常量集合；写 events 时若 kind 不在枚举内 → 单测期 fail，运行期降级为 `level=warning` 但仍写入（不抛异常）。
