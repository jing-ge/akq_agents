"""``factor.promote_shadows``：每日盘后跑 shadow 因子的 OOS 评估与晋升/降级判定。

之前 ``_promote_shadows`` 是耦合在 ``DiscoveryEngine.run_batch`` 主流程**中段**调用的;
当 ``_prepare_data`` 返回 empty (例如凌晨 today 数据还没刷) 时, run_batch 直接 return,
``_promote_shadows`` 根本不会被调用 → shadow 因子 OOS 计数永远是 NULL, 拖延晋升。

拆出来独立 daily 跑, 让"采样新候选"与"推进已有 shadow 的 OOS 计数"两件事互不阻塞。

调度时间: daily 17:30 (盘后 data.refresh 16:00 + post_close 16:30 后, 数据已就绪)。
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

JOB_ID = "factor.promote_shadows"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册到 APScheduler。需要 services 提供 discovery_engine。"""
    job_cfg = cfg.jobs.factor_promote_shadows
    if not job_cfg.enabled:
        return
    if "discovery_engine" not in services:
        logger.info("factor.promote_shadows enabled but discovery_engine missing; skip")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)

    scheduler.add_job(
        _run,
        CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def _do(services: dict[str, Any]) -> dict[str, Any]:
    """跑一次 shadow OOS 评估; 返回 stats 供 events 记账."""
    from akq_agents.services.factors.discovery import DiscoveryStats

    engine = services["discovery_engine"]
    stats = DiscoveryStats()
    today = date.today()
    try:
        engine._promote_shadows(stats=stats, as_of_date=today)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.exception("factor.promote_shadows failed: %s", exc)
        raise

    return {
        "promoted": stats.promoted,
        "demoted": stats.demoted,
        "as_of_date": today.isoformat(),
    }
