"""``data.refresh_daily``：交易日今日 OHLCV 数据自动拉取。

策略（针对 A 股数据源就绪时间不确定 + 偶发失败）：
- 注册多个 cron 触发器，从 first_try_hour:first_try_minute 起
  每 ``retry_interval_minutes`` 分钟一次，直到 ``stop_hour``:00
- 每次触发的执行体先检查今天数据是否已就绪（quality_passed），就绪则直接 skip
- 调用 ``repository.refresh_daily_fast``（批量接口，~15s）
- 任何异常都不抛，写到 events 表

为什么用多个 cron 而非单个 interval：
- daemon 在白天可能停启，interval 计时会乱
- cron 每次绝对时间触发，更可预测

仅在交易日跑（JobRunner 自带交易日护栏）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner
from akq_agents.services.data.exceptions import QualityCheckFailed

logger = logging.getLogger(__name__)

JOB_ID = "data.refresh_daily"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.data_refresh
    if not job_cfg.enabled:
        return
    if "data_repository" not in services:
        logger.info("data.refresh_daily enabled but data_repository missing; skip")
        return

    # 生成所有触发时间点：first_try → stop_hour:00（不含 stop_hour:00）
    # 例如 first=16:00, retry=30min, stop=22 → [16:00, 16:30, ..., 21:30]
    minutes_in_window = (job_cfg.stop_hour - job_cfg.first_try_hour) * 60 - job_cfg.first_try_minute
    n_triggers = max(1, minutes_in_window // job_cfg.retry_interval_minutes)

    trigger_times: list[tuple[int, int]] = []
    base_min = job_cfg.first_try_hour * 60 + job_cfg.first_try_minute
    for i in range(n_triggers):
        total = base_min + i * job_cfg.retry_interval_minutes
        h, m = divmod(total, 60)
        if h >= job_cfg.stop_hour:
            break
        trigger_times.append((h, m))

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)

    # 把所有触发时间合并成一个 cron 表达式（hour=16,16,17... minute=0,30,0...）
    # APScheduler CronTrigger 不支持 minute 列表跨 hour，所以为每个 (h,m) 单独注册一个 job
    for _i, (h, m) in enumerate(trigger_times):
        scheduler.add_job(
            _run,
            CronTrigger(hour=h, minute=m),
            id=f"{JOB_ID}@{h:02d}{m:02d}",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=None,
        )

    logger.info(
        "data.refresh_daily registered with %d cron triggers (%s ~ %s)",
        len(trigger_times),
        f"{trigger_times[0][0]:02d}:{trigger_times[0][1]:02d}" if trigger_times else "?",
        f"{trigger_times[-1][0]:02d}:{trigger_times[-1][1]:02d}" if trigger_times else "?",
    )


def _do(services: dict[str, Any]) -> dict[str, Any]:
    """执行体：先检查就绪，再拉。"""
    import time as _time
    repo = services["data_repository"]
    today = date.today()

    # 提前判定：今天已经成功拉过且通过质量门 → skip（避免浪费）
    cached = _check_cached(repo, today)
    if cached is not None:
        logger.info(
            "data.refresh_daily: SKIP already_fetched_today target=%s cached_rows=%d",
            today.isoformat(), cached,
        )
        return {
            "skipped": True, "reason": "already_fetched_today",
            "target_date": today.isoformat(), "cached_rows": cached,
        }

    logger.info("data.refresh_daily: START target=%s", today.isoformat())
    _t0 = _time.monotonic()
    # 真正拉取（refresh_daily_fast 内部还会再做一次 cache 检查 = 双保险）
    result = repo.refresh_daily_fast(today)
    payload = {
        "skipped": False,
        "target_date": str(result.target_date),
        "fetched": result.fetched,
        "requested": result.requested,
        "cached_hit": result.cached_hit,
        "failed": result.failed,
        "quality_passed": result.quality_passed,
        "duration_s": result.duration_s,
    }
    logger.info(
        "data.refresh_daily: DONE target=%s fetched=%d requested=%d cached_hit=%d failed=%d quality_passed=%s duration=%.1fs elapsed=%.1fs",
        result.target_date, result.fetched, result.requested, result.cached_hit,
        result.failed, result.quality_passed, result.duration_s, _time.monotonic() - _t0,
    )
    # quality_passed=False (akshare 接口异常 / 数据空 / schema drift) 必须 raise，
    # 否则 JobRunner 会把它当 'ok' 写入 → alerter 看不到 + 后续 retry cron 被幂等吞掉。
    if not result.quality_passed and not getattr(result, "skipped_non_trading_day", False):
        logger.warning("data.refresh_daily: quality check FAILED target=%s", today.isoformat())
        raise QualityCheckFailed({"refresh_daily_fast": False})
    return payload


def _check_cached(repo, d: date) -> int | None:
    """看 refresh_state 里今天是否已经成功，返回缓存的 rows 数；否则 None。"""
    try:
        return repo._refresh_state_rows(d)
    except Exception:
        return None


def run_once_now(runner: JobRunner, services: dict[str, Any]) -> dict[str, Any]:
    """供 CLI / Web 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=600)
