"""``factor.discovery``：定时跑因子自动发现引擎。

默认每 60 分钟跑一次，每次抽样 ``n_candidates`` 个新候选评估，通过门槛者注册进 registry
并写入 ``factor_proposals`` 表。

为什么放 IntervalTrigger 而不是 cron：用户目标是「agent 24h 不断产因子」。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "factor.discovery"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.factor_discovery
    if not job_cfg.enabled:
        return
    if not _has_required_services(services):
        logger.info("factor.discovery enabled but discovery_engine missing; skip registration")
        return

    n_candidates = getattr(job_cfg, "n_candidates_per_run", 20)

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(
            JOB_ID,
            partition,
            lambda: _do(services, n_candidates=n_candidates),
            timeout_s=job_cfg.timeout_s,
        )

    scheduler.add_job(
        _run,
        IntervalTrigger(minutes=job_cfg.interval_minutes),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def run_once_now(runner: JobRunner, services: dict[str, Any], n_candidates: int = 20) -> dict:
    """供 CLI / Web 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(
        JOB_ID,
        partition,
        lambda: _do(services, n_candidates=n_candidates),
        timeout_s=600,
    )


def _has_required_services(services: dict[str, Any]) -> bool:
    return "discovery_engine" in services


def _do(services: dict[str, Any], *, n_candidates: int) -> dict[str, Any]:
    engine = services["discovery_engine"]
    stats = engine.run_batch(n_candidates=n_candidates)
    return stats.as_dict()
