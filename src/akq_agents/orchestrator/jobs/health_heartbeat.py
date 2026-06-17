"""``health.heartbeat``：每 5 分钟更新 ``data/daemon_state.json`` 的 last_heartbeat。

关键边界（spec §4）：
- **不经 JobRunner**：避免在 job_runs 表灌水（每 5 分钟一条记录 = 8640 行/月）
- **不写 events**：理由同上
- 仅原地更新文件，失败 swallow（不影响 daemon 主循环）
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.daemon_state_file import DaemonStateFile

logger = logging.getLogger(__name__)

JOB_ID = "health.heartbeat"


def register(
    scheduler: BackgroundScheduler,
    cfg: SchedulerConfig,
    daemon_state_file: DaemonStateFile,
) -> None:
    job_cfg = cfg.jobs.health_heartbeat
    if not job_cfg.enabled:
        return

    def _tick() -> None:
        try:
            daemon_state_file.update_heartbeat()
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat update failed (swallow): %s", exc)

    scheduler.add_job(
        _tick,
        IntervalTrigger(minutes=job_cfg.interval_minutes),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )
