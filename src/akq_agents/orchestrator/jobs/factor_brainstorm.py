"""``factor.brainstorm``：每日 20:00 cron，让 LLM 提因子构建方向。

产出写入 ``factor_proposals`` 表 status='llm_suggested'，需人工在 /research 页面
审核。审核接受后 status → 'shadow'，下一轮 factor.discovery 会接管做 OOS 评估。

仅在交易日跑（JobRunner trading-day 白名单）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "factor.brainstorm"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.factor_brainstorm
    if not job_cfg.enabled:
        return
    if "llm_factor_brainstormer" not in services:
        logger.info("factor.brainstorm enabled but llm_factor_brainstormer missing; skip")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(
            JOB_ID,
            partition,
            lambda: _do(services, n=job_cfg.n_suggestions),
            timeout_s=job_cfg.timeout_s,
        )

    scheduler.add_job(
        _run,
        CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    logger.info("factor.brainstorm registered at %02d:%02d, n=%d",
                job_cfg.hour, job_cfg.minute, job_cfg.n_suggestions)


def run_once_now(runner: JobRunner, services: dict[str, Any], n: int = 20) -> Any:
    """供 web /api/research/factors/brainstorm/run 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(
        JOB_ID, partition,
        lambda: _do(services, n=n),
        timeout_s=120,
    )


def _do(services: dict[str, Any], *, n: int) -> dict[str, Any]:
    brainstormer = services["llm_factor_brainstormer"]
    stats = brainstormer.run(n=n)
    # 全失败时 raise 让 JobRunner 写 status='failed'，避免 /ops 看板把全失败显示成 ok
    errors = int(stats.get("errors", 0) or 0)
    accepted = int(stats.get("accepted_into_review", 0) or 0)
    if errors > 0 and accepted == 0:
        raise RuntimeError(f"LLM brainstorm produced 0 proposals (errors={errors}): {stats}")
    return stats
