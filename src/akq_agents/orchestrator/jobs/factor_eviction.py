"""``factor.eviction``: weekly cron 淘汰低分因子.

M19: 防止 factor_proposals 池子无限膨胀. 默认周一 03:00 跑一次, 删 score<0.05 +
超 300 上限的因子. 跟 batch.deep_research / factor.discovery cron 在不同时段,
互不干扰。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "factor.eviction"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册 weekly cron — day_of_week='mon' hour=3 (默认)."""
    job_cfg = cfg.jobs.factor_eviction
    if not job_cfg.enabled:
        return
    if "data_repository" not in services:
        logger.info("factor.eviction enabled but data_repository missing; skip")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services, cfg), timeout_s=job_cfg.timeout_s)

    scheduler.add_job(
        _run,
        CronTrigger(day_of_week=job_cfg.day_of_week, hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def _do(services: dict[str, Any], cfg: SchedulerConfig, *, dry_run: bool = False) -> dict[str, Any]:
    """跑一次 eviction. dry_run=True 时只统计不删."""
    from akq_agents.services.factors.eviction import EvictionConfig, evict_factors

    repo = services["data_repository"]
    state_store = services.get("scheduler_state_store")
    meta_db = Path(repo._base_dir) / "meta.db"

    job_cfg = cfg.jobs.factor_eviction
    ev_cfg = EvictionConfig(
        max_pool_size=job_cfg.max_pool_size,
        min_score=job_cfg.min_score,
        new_factor_grace_days=job_cfg.new_factor_grace_days,
        dry_run=dry_run,
    )
    return evict_factors(meta_db_path=meta_db, state_store=state_store, cfg=ev_cfg)
