"""``factor.code_brainstorm`` (重构新增): 每日 cron, 让 LLM 自由出 Python 代码因子.

与 ``factor.brainstorm`` (DSL 受限) 的区别:
- 不限定 base × op × window × direction 笛卡尔积, LLM 写任何 sandbox 允许的
  Python ``def compute(ohlcv) -> pd.Series`` 都接受
- 跨 session 同源代码 sha1 去重, 避免反复入库相同思路
- 走 sandbox 编译: 危险 / 编译失败 / 超时 都被静默跳过
- 产出写入 ``factor_proposals`` recipe_kind='code', 同样走人工审核 + OOS 评估

调度时间: daily 21:00 (晚于 factor.brainstorm 20:00, 错开 LLM gateway 限速窗口).
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

JOB_ID = "factor.code_brainstorm"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册到 APScheduler。需要 services 提供 ``llm_code_factor_brainstormer``。"""
    job_cfg = cfg.jobs.factor_code_brainstorm
    if not job_cfg.enabled:
        return
    if "llm_code_factor_brainstormer" not in services:
        logger.info("factor.code_brainstorm enabled but llm_code_factor_brainstormer missing; skip")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(
            JOB_ID, partition,
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
    logger.info(
        "factor.code_brainstorm registered at %02d:%02d, n=%d",
        job_cfg.hour, job_cfg.minute, job_cfg.n_suggestions,
    )


def run_once_now(runner: JobRunner, services: dict[str, Any], n: int = 10) -> Any:
    """供 web /api/research/factors/code-brainstorm/run 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(
        JOB_ID, partition,
        lambda: _do(services, n=n),
        timeout_s=300,  # P0-1: 与 cron 路径 (job_cfg.timeout_s=300) 对齐
    )


def _do(services: dict[str, Any], *, n: int) -> dict[str, Any]:
    """跑一次 code brainstorm; 返回 stats 供 events 记账."""
    brainstormer = services["llm_code_factor_brainstormer"]
    stats = brainstormer.run(n=n)
    # 全失败时 raise 让 JobRunner 写 status='failed'，避免 /ops 看板把全失败显示成 ok
    errors = int(stats.get("errors", 0) or 0)
    accepted = int(stats.get("accepted_into_review", 0) or 0)
    if errors > 0 and accepted == 0:
        raise RuntimeError(f"LLM code brainstorm produced 0 proposals (errors={errors}): {stats}")
    return stats
