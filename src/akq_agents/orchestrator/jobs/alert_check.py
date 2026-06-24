"""``alert.check``：每 30 分钟巡检关键指标，触发告警 (M17)。

不走 trading_day 护栏：非交易日 alerter 也要继续 watch（NAV 异动可能延迟显现）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

JOB_ID = "alert.check"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.alerter
    if not job_cfg.enabled:
        return
    if "alerter" not in services:
        return  # 没装配就跳过

    def _run() -> None:
        # partition 用 30 分钟桶，防瞬时双触发
        now = datetime.now()
        bucket_minute = (now.minute // 30) * 30
        partition = now.strftime(f"%Y-%m-%dT%H:{bucket_minute:02d}")
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)

    scheduler.add_job(
        _run,
        IntervalTrigger(minutes=job_cfg.interval_minutes),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def _do(services: dict[str, Any]) -> dict[str, Any]:
    alerter = services["alerter"]
    return alerter.run_check()
