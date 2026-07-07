"""``board.refresh_daily``：交易日盘后自动抓取当日行业板块快照。

数据源同花顺行业板块（``BoardRepository.refresh_board_daily``），只给当日数据，
每日抓一次落地，历史随天数累积（供板块轮动热力图）。

策略照抄 ``data_refresh``：单个 cron 触发（默认 16:35，晚于 OHLCV 刷新），
仅交易日跑（JobRunner 自带交易日护栏）；已成功过则命中缓存 skip。

``BoardRepository`` 从 ``services["data_repository"]`` 现构造（复用其 gateway /
calendar / 路径），与 web/api/board.py 同一套构造方式，不额外挂 services。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner
from akq_agents.services.data.exceptions import FetchError

logger = logging.getLogger(__name__)

JOB_ID = "board.refresh_daily"


def _build_board_repo(services: dict[str, Any]):
    """从 data_repository 复用 gateway / calendar / 路径构造 BoardRepository。"""
    from akq_agents.services.data.board_repository import BoardRepository

    repo = services["data_repository"]
    return BoardRepository(
        gateway=repo._gateway,
        calendar=repo._calendar,
        base_dir=repo._base_dir,
        meta_db_path=repo.meta_db_path,
    )


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.board_refresh
    if not job_cfg.enabled:
        return
    if "data_repository" not in services:
        logger.info("board.refresh_daily enabled but data_repository missing; skip")
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
    logger.info(
        "board.refresh_daily registered (cron %02d:%02d)", job_cfg.hour, job_cfg.minute
    )


def _do(services: dict[str, Any]) -> dict[str, Any]:
    board_repo = _build_board_repo(services)
    today = date.today()

    cached = board_repo._refresh_state_rows(today)
    if cached is not None:
        logger.info("board.refresh_daily: SKIP already_fetched target=%s rows=%d", today, cached)
        return {"skipped": True, "reason": "already_fetched_today",
                "target_date": today.isoformat(), "cached_rows": cached}

    logger.info("board.refresh_daily: START target=%s", today.isoformat())
    try:
        result = board_repo.refresh_board_daily(today)
    except FetchError as exc:
        logger.warning("board.refresh_daily: fetch FAILED target=%s: %s", today, exc)
        raise  # 让 JobRunner 记 failed + alerter 可见

    payload = result.as_dict()
    logger.info(
        "board.refresh_daily: DONE target=%s rows=%d quality_passed=%s duration=%.1fs",
        result.target_date, result.rows, result.quality_passed, result.duration_s,
    )
    # 质量门不过（板块数过少 / 接口异常）→ raise，避免被当 ok 幂等吞掉
    if not result.quality_passed and not result.skipped_non_trading_day:
        raise FetchError(reason_code="UNKNOWN", message=f"board quality failed: {result.rows} boards")
    return payload


def run_once_now(runner: JobRunner, services: dict[str, Any]) -> dict[str, Any]:
    """供 CLI / Web 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=300)
