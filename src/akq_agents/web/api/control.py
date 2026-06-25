"""Control endpoints：从 Web 控制台操控后台。

提供：
- ``POST /jobs/{name}/trigger`` — 同步触发某个 job（C5 走 JobRunner）
- ``POST /data/refresh`` — 异步触发今日 OHLCV 数据拉取（后台线程，立即返回）
- ``GET  /data/refresh/status`` — 查询当前后台拉取进度

支持的 jobs：``batch.post_close``、``batch.deep_research``、``factor.discovery``。

I1 替代 A: daemon 生命周期（start/stop）由 start.sh 直接管理，不再走 web API。
原因：web 进程不该负责"拉起 daemon 子进程"这种运维职责。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from akq_agents.web.deps import ServiceContainer, get_services

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- manual job trigger --------------------------------------------


_SUPPORTED_JOBS = {"batch.post_close", "batch.deep_research", "factor.discovery"}


@router.post("/jobs/{name}/trigger")
async def trigger_job(name: str, n_candidates: int = 20) -> dict[str, Any]:
    """同步触发 job。C5: 走 JobRunner 写 job_runs/events，与 daemon cron 路径一致。"""
    if name not in _SUPPORTED_JOBS:
        raise HTTPException(404, f"unknown job: {name}")
    svc: ServiceContainer = get_services()
    if svc.job_runner is None:
        raise HTTPException(503, "job_runner not ready (web container 未装配)")
    partition = date.today().isoformat()

    if name == "batch.post_close":
        if svc.workflow is None:
            raise HTTPException(503, "workflow not ready")
        # M11: 加上 step recorder 让 UI 能看见子步骤
        recorder = None
        try:
            from akq_agents.orchestrator.step_recorder import StepRecorder
            repo = svc.workflow.services.get("data_repository")
            if repo is not None:
                recorder = StepRecorder(
                    repo._base_dir / "meta.db",
                    parent_job_id="batch.post_close",
                    parent_partition=partition,
                )
        except Exception:  # noqa: BLE001
            recorder = None

        from akq_agents.orchestrator.jobs.batch_post_close import _do, JOB_ID
        # 把 recorder 通过 services 临时传入（避免改 _do 签名）
        ws_services = dict(svc.workflow.services)
        if recorder is not None:
            ws_services["__recorder__"] = recorder  # batch_post_close._make_recorder 会忽略这个，按需扩展
        result = svc.job_runner.run(JOB_ID, partition, lambda: _do(ws_services), timeout_s=5400)
        return {
            "status": result.status,
            "reason_code": result.reason_code,
            "payload": result.payload,
        }

    if name == "batch.deep_research":
        from akq_agents.orchestrator.jobs.batch_deep_research import _do, JOB_ID

        ws_services = svc.workflow.services if svc.workflow else {}
        result = svc.job_runner.run(JOB_ID, partition, lambda: _do(ws_services), timeout_s=5400)
        return {
            "status": result.status,
            "reason_code": result.reason_code,
            "payload": result.payload,
        }

    if name == "factor.discovery":
        engine = svc.discovery_engine
        if engine is None:
            raise HTTPException(503, "discovery_engine not ready")
        from akq_agents.orchestrator.jobs.factor_discovery import JOB_ID

        def _do_discovery() -> dict[str, Any]:
            stats = engine.run_batch(n_candidates=n_candidates, as_of_date=date.today())
            return stats.as_dict()

        result = svc.job_runner.run(JOB_ID, partition, _do_discovery, timeout_s=900)
        return {
            "status": result.status,
            "reason_code": result.reason_code,
            "payload": result.payload,
        }

    raise HTTPException(500, "unreachable")


# ============================================================
# 数据拉取（异步后台线程，立即返回）
# ============================================================

# 进程级单例：避免重复触发
_data_refresh_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "target_date": None,
    "result": None,
    "error": None,
}
_data_refresh_lock = threading.Lock()


@router.post("/data/refresh")
async def data_refresh_trigger(target_date: str | None = None) -> dict[str, Any]:
    """异步触发今日 OHLCV 数据拉取。

    立即返回 (running=True)，真正工作在后台线程跑（通常 5-30 分钟）。
    用 GET /data/refresh/status 轮询。

    target_date 不传则用今天。
    """
    svc: ServiceContainer = get_services()
    if svc.repo is None:
        raise HTTPException(503, "data_repository not ready")

    # 校验 target_date 必须在抢锁之前，否则非法日期会把 running 永久卡 True
    if target_date:
        try:
            tdate = date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(400, f"invalid target_date: {target_date!r}, expect YYYY-MM-DD")
    else:
        tdate = date.today()

    with _data_refresh_lock:
        if _data_refresh_state["running"]:
            return {
                "status": "already_running",
                "started_at": _data_refresh_state["started_at"],
                "target_date": _data_refresh_state["target_date"],
            }
        _data_refresh_state.update({
            "running": True,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "target_date": tdate.isoformat(),
            "result": None,
            "error": None,
        })

    repo = svc.repo

    def _worker():
        try:
            # 使用快速路径（stock_zh_a_spot 一次性拉全市场快照，~15s vs 单股逐拉 ~30min）
            result = repo.refresh_daily_fast(tdate)
            with _data_refresh_lock:
                _data_refresh_state.update({
                    "running": False,
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "result": {
                        "target_date": str(result.target_date),
                        "requested": getattr(result, "requested", None),
                        "fetched": getattr(result, "fetched", None),
                        "cached_hit": getattr(result, "cached_hit", None),
                        "failed": getattr(result, "failed", None),
                        "quality_passed": getattr(result, "quality_passed", None),
                        "skipped_non_trading_day": getattr(result, "skipped_non_trading_day", False),
                        "duration_s": getattr(result, "duration_s", None),
                    },
                })
        except Exception as exc:  # noqa: BLE001
            logger.exception("data.refresh failed")
            with _data_refresh_lock:
                _data_refresh_state.update({
                    "running": False,
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "error": f"{type(exc).__name__}: {exc!s}"[:500],
                })

    t = threading.Thread(target=_worker, daemon=True, name=f"data-refresh-{tdate}")
    t.start()

    return {
        "status": "started",
        "target_date": str(tdate),
        "started_at": _data_refresh_state["started_at"],
        "hint": "后台正在拉取，请用 GET /api/control/data/refresh/status 轮询",
    }


@router.get("/data/refresh/status")
async def data_refresh_status() -> dict[str, Any]:
    """查询当前数据拉取后台线程状态。"""
    with _data_refresh_lock:
        return dict(_data_refresh_state)
