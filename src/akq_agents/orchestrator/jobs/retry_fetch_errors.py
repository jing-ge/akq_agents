"""``retry.fetch_errors``：每 5 分钟扫一遍 ``fetch_errors`` 表，重试未解决记录。

包装 P1 :class:`RetryWorker.run_once`。trading_day 护栏白名单**绕过**：retry 在
非交易日也需要继续清理积压。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "retry.fetch_errors"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.retry_fetch_errors
    if not job_cfg.enabled:
        return
    if "retry_worker" not in services:
        # 没装配 retry_worker 就不注册（避免假装能跑）
        return

    def _run() -> None:
        # partition 用时间窗起点，保证 5 分钟内重复触发也幂等
        now = datetime.now()
        partition = now.strftime("%Y-%m-%dT%H:%M")
        partition = partition[:-1] + "0"  # 落到分钟级 5 分钟桶（粗略，仅防瞬时双触发）
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
    worker = services["retry_worker"]
    stats = worker.run_once()
    resolved = stats.get("resolved", 0)
    scanned = stats.get("scanned", 0)
    # scanned=0 时 (最常见的空跑) 用 DEBUG 不刷屏; 只要有活干就 INFO.
    if scanned > 0 or resolved > 0:
        logger.info(
            "retry.fetch_errors: scanned=%d resolved=%d",
            scanned, resolved,
        )
    else:
        logger.debug("retry.fetch_errors: idle (scanned=0 resolved=0)")
    return {"resolved": resolved, "scanned": scanned}
