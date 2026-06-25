# P2 调度守护使用指南

> 对应设计文档：`docs/superpowers/specs/2026-06-17-p2-scheduling-daemon-design.md`

## 1. 模块总览

```
src/akq_agents/orchestrator/
├── __init__.py
├── workflow.py                # 不动；被 batch.post_close 调用
├── scheduler.py               # QuantDaemon：进程主入口
├── job_runner.py              # JobRunner：幂等 + 超时 + 护栏 + 记账
├── state_store.py             # SchedulerStateStore：job_runs/events 读写 + events.kind 注册表
├── daemon_state_file.py       # DaemonStateFile：data/daemon_state.json 原子读写
├── signal_handler.py          # self_heal_on_boot + GracefulShutdown + mark_daemon_*
└── jobs/                      # 每个 job 一个文件
    ├── __init__.py
    ├── batch_post_close.py    # 15:30 盘后 cron
    ├── batch_deep_research.py # 周日 22:00 cron（P3 接入前为 noop 空实现）
    ├── retry_fetch_errors.py  # 每 5 分钟（包装 P1 RetryWorker）
    └── health_heartbeat.py    # 每 5 分钟（仅更新 data/daemon_state.json）
```

入口配置：`config/scheduler.yaml`  
入口模型：`src/akq_agents/models/scheduler_config.py:SchedulerConfig`

## 2. 存储布局

P2 复用 P1 的 `data/meta.db`（P1 附录 B §6 已强制 WAL + busy_timeout=5000）。新增表：

- `job_runs` — 每个 (job_id, partition) 一行；记录 status / started_at / finished_at / duration_ms / payload_json
- `events` — 事件流；级别 info/warning/error；kind 命名见下表

另：
- `data/daemon_state.json` — 单进程状态（不入 db），含 status / pid / started_at / last_heartbeat / version

## 3. CLI 速查

```bash
# 前台启动 daemon；Ctrl+C 触发优雅停机
PYTHONPATH=src python -m akq_agents.cli.app daemon start

# 读 data/daemon_state.json + 心跳判定，输出 JSON
PYTHONPATH=src python -m akq_agents.cli.app daemon status

# 查看最近 20 个任务运行
PYTHONPATH=src python -m akq_agents.cli.app daemon runs --last 20

# 仅查 batch.post_close 的失败记录
PYTHONPATH=src python -m akq_agents.cli.app daemon runs --job-id batch.post_close --status failed

# 查看最近事件（按级别过滤）
PYTHONPATH=src python -m akq_agents.cli.app daemon events --last 20 --level warning
PYTHONPATH=src python -m akq_agents.cli.app daemon events --kind-prefix batch.
```

## 4. Job 列表

| Job ID | 触发 | trading_day 护栏 | 期望耗时 | 备注 |
|---|---|---|---|---|
| `batch.post_close` | 15:30，仅交易日 | ✓ | < 90min | 调 `QuantWorkflow.run_once`；P3 接入后含组合 pipeline + AnalystAgent |
| `batch.deep_research` | 周日 22:00 | ✓ | < 90min | P2 阶段为空实现；P3 接入后跑因子有效性滚动评估 |
| `retry.fetch_errors` | 每 5 分钟 | ✗（白名单绕过） | < 60s | 包装 P1 `RetryWorker.run_once` |
| `health.heartbeat` | 每 5 分钟 | ✗（白名单绕过） | < 1s | 仅更新 `data/daemon_state.json`，**不写 job_runs/events**（避免表灌水） |

## 5. JobRunner 行为

每次 `runner.run(job_id, partition, fn, timeout_s=N)` 经如下步骤：

1. **幂等检查**：`(job_id, partition)` 已 status='ok' → 返回 `noop`，fn 不被调用
2. **trading_day 护栏**：job_id 在 `DEFAULT_TRADING_DAY_REQUIRED` 白名单时，今天非交易日 → 返回 `skipped`，发 `<job_id>.skipped` event
3. **执行**：`ThreadPoolExecutor.submit(fn)`，超时 `Future.result(timeout)`
4. **超时**（不强杀）：写 `status=timeout` + `<job_id>.timeout` event（level=warning）
5. **异常分类**：
   - `DataNotReady`（P1）→ `status=skipped`, reason_code=`DATA_NOT_READY`, event level=info
   - 其它任何异常 → `status=failed`, reason_code=`UNKNOWN`, event level=error，含 traceback
6. **成功**：写 `status=ok` + `<job_id>.completed` event（payload 含 duration_ms + fn 返回 dict）

**永不抛出**：JobRunner 把所有异常吞掉，让调度器主循环不被业务异常中断。

## 6. 启动期 self_heal

`QuantDaemon.start()` 开头会运行 `self_heal_on_boot`：

1. 扫描 `job_runs` 中 `status IN ('running','interrupted') AND started_at < now-6h` → 标 `crashed`，发 `<job_id>.crashed` event
2. 仅对 `batch.post_close`：若今天是交易日 + 现在已过 15:30 + 当日 partition 不是 ok → 立即触发补跑
3. retention 清理：`events_days=30`、`job_runs_days=90` 之外的记录删除

之后只有启动时跑这一次；不再每日定时 self_heal（v1 设计的 02:00 cron 已砍）。

## 7. 优雅停机

SIGTERM / SIGINT / `daemon.request_stop()` 触发：

1. `BackgroundScheduler.shutdown(wait=False)` 立即返回，停止新任务
2. 抓取所有 `status='running'` 的 job_runs，按 job_id 分组发 `<job_id>.interrupted` event
3. `UPDATE job_runs SET status='interrupted' WHERE status='running'`
4. 写 `daemon.stopped` event
5. `daemon_state.json.status = 'stopped'`

> Python 线程无法强杀；运行中的业务函数会自然跑完（或被下次启动时通过 self_heal 当作 crashed 处理）。

## 8. events.kind 命名规范（P2 附录 C 强制约束）

格式：`<domain>.<noun>.<verb_past>`，全部小写 + 点分。

完整注册表见 `src/akq_agents/orchestrator/state_store.py:KNOWN_EVENT_KIND_PREFIXES`（按域前缀放行）。写入未匹配任何前缀的 kind 会发 warning 但仍然落库，便于前向兼容。

域：`batch / retry / data / factor / portfolio / analyst / chat / llm / daemon`  
动词过去分词：`completed / failed / skipped / timeout / crashed / interrupted / started / stopped / generated / degraded / activated / deactivated / bootstrap / evaluated / missing / blocked / unknown`

## 9. 配置示例（`config/scheduler.yaml`）

```yaml
scheduler:
  timezone: "Asia/Shanghai"
  thread_pool_size: 4
  shutdown_grace_s: 30
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
      timeout_s: 5
  retention:
    events_days: 30
    job_runs_days: 90
```

## 10. 故障排查

### `daemon status` 显示 `is_alive=false` 但 daemon 进程在跑
- 检查 heartbeat 是否被注册（`health_heartbeat.enabled=true`）
- 看 `data/daemon_state.json.last_heartbeat` 时间戳；超过 10 分钟（2 个 heartbeat 周期）即判死

### 同一交易日重复触发 `batch.post_close`
P2 设计就是幂等：第二次直接 `noop`，不重新执行；`job_runs` 表中 `(job_id, partition)` 唯一索引保证。

### `daemon runs` 看到 `status='crashed'` 的记录
说明上次进程被 `kill -9` 或宿主机重启，启动期 self_heal 把残留 running 转 crashed。如果 partition 是今日 + 已过 15:30，self_heal 会自动补跑一次。

### `events` 表灌水
P2 设计：`heartbeat` 不写 events，`retry` 每 5 分钟一条，`batch` 每天 1-2 条。如发现 events 在短时间内激增，多半是某个 job 反复失败；用 `daemon events --kind-prefix <job>.failed --last 50` 查。

### `meta.db` 写入冲突 / SQLITE_BUSY
P1 附录 B §6 已承诺 WAL + busy_timeout=5000；理论上 5s 内的锁竞争都会自动重试。如仍出现，检查是否有别的进程（如旧版本 daemon）也在写。

## 11. 与 P1 / P3 / P4 / P5 的接口承诺

详见 spec §附录 A/B。简要：

- 严格依赖 P1：`refresh_daily` 幂等、`is_trading_day` 准确、WAL + busy_timeout=5000
- 承诺给下游：`job_runs` / `events` schema 稳定、`daemon_state.json` 字段稳定、JobRunner 是新 job 的唯一注册入口、events.kind 注册表稳定

## 12. 验收快查

| Spec | 状态 | 验证方式 |
|---|---|---|
| A1 daemon start/status | ✅ | `tests/orchestrator/test_daemon_lifecycle.py::test_daemon_status_payload` |
| A2 非交易日 skip | ✅ | `test_job_runner.py::test_run_trading_day_guard_skips_non_trading_day` |
| A3 同日 batch 幂等 | ✅ | `test_job_runner.py::test_run_idempotent_when_already_ok` |
| A4 kill -9 → crashed | ✅ | `test_daemon_lifecycle.py::test_self_heal_on_boot_marks_old_running_crashed` |
| A5 events kind=completed | ✅ | `test_jobs_post_close.py::test_run_once_now_writes_completed_event` |
| A6 SIGTERM 优雅停机 | ✅ | `test_daemon_lifecycle.py::test_interrupted_marker_on_shutdown` |
| A7 retry 非交易日仍跑 | ✅ | `test_job_runner.py::test_run_trading_day_guard_only_for_whitelist` |
| A8 retry 消费 fetch_errors | ⏸ | 集成测试需 RetryWorker mock；与 P1 RetryWorker 通过单测验证 |
| A9 events 写入失败 fallback | ✅ | `test_state_store.py::test_write_event_db_failure_falls_back_to_stderr` |
| A10 heartbeat 不灌 job_runs | ✅ | 设计：heartbeat 不经 JobRunner（spec §4 关键边界） |
| B1 覆盖率 ≥ 80% | ✅ 91% | `pytest --cov=akq_agents.orchestrator` |
| B2 ruff 0 warnings | ✅ | `ruff check src/akq_agents/orchestrator/ tests/orchestrator/` |
| B5 events.kind 注册表 | ✅ | `test_state_store.py::test_known_event_kinds_includes_all_needed` |
| C4 WAL 模式生效 | ✅ | `tests/data/test_repository.py::test_meta_db_wal_mode_enabled` |
