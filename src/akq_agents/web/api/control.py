"""Control endpoints：从 Web 控制台操控后台。

提供：
- ``POST /daemon/start`` — 后台启动 daemon（subprocess + pid 文件）
- ``POST /daemon/stop`` — 给 daemon pid 发 SIGTERM
- ``POST /jobs/{name}/trigger`` — 同步触发某个 job
- ``POST /data/refresh`` — 异步触发今日 OHLCV 数据拉取（后台线程，立即返回）
- ``GET  /data/refresh/status`` — 查询当前后台拉取进度

支持的 jobs：``batch.post_close``、``batch.deep_research``、``factor.discovery``。

为什么 daemon 用 subprocess 而不是 in-process：
- daemon 包含 APScheduler 和阻塞 wait 循环，跟 FastAPI 同进程会互相干扰；
- 用 subprocess 隔离后，web 可以独立崩溃/重启，daemon 不受影响；
- pid 由 ``daemon_state.json`` + 系统 pid 双重确认。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from akq_agents.web.deps import ServiceContainer, get_services

logger = logging.getLogger(__name__)
router = APIRouter()


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DAEMON_PID_FILE = _PROJECT_ROOT / "data" / "daemon.pid"
_DAEMON_LOG_FILE = _PROJECT_ROOT / "data" / "daemon.log"


# ---------- daemon lifecycle ----------------------------------------------


@router.post("/daemon/start")
async def daemon_start() -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.daemon_state_file is not None and svc.daemon_state_file.is_alive(max_age_s=600):
        return {"status": "already_running", "pid": _read_pid()}

    _DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(_DAEMON_LOG_FILE, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "akq_agents.cli.app", "daemon", "start"],
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT / "src")},
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    _DAEMON_PID_FILE.write_text(str(proc.pid))
    return {"status": "starting", "pid": proc.pid, "log": str(_DAEMON_LOG_FILE)}


@router.post("/daemon/stop")
async def daemon_stop() -> dict[str, Any]:
    pid = _read_pid()
    if pid is None:
        return {"status": "not_running"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _DAEMON_PID_FILE.unlink(missing_ok=True)
        return {"status": "not_running"}
    return {"status": "stopping", "pid": pid}


def _read_pid() -> int | None:
    if not _DAEMON_PID_FILE.exists():
        return None
    try:
        pid = int(_DAEMON_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0：检查存活
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # 死 pid → 清理文件，避免下次误判 already_running
        try:
            _DAEMON_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return None


# ---------- manual job trigger --------------------------------------------


_SUPPORTED_JOBS = {"batch.post_close", "batch.deep_research", "factor.discovery"}


@router.post("/jobs/{name}/trigger")
async def trigger_job(name: str, n_candidates: int = 20) -> dict[str, Any]:
    """同步触发 job。"""
    if name not in _SUPPORTED_JOBS:
        raise HTTPException(404, f"unknown job: {name}")
    svc: ServiceContainer = get_services()

    if name == "batch.post_close":
        if svc.workflow is None:
            raise HTTPException(503, "workflow not ready")
        # M11: 加上 step recorder 让 UI 能看见子步骤
        recorder = None
        try:
            from akq_agents.orchestrator.step_recorder import StepRecorder
            from datetime import date as _date
            repo = svc.workflow.services.get("data_repository")
            if repo is not None:
                recorder = StepRecorder(
                    repo._base_dir / "meta.db",
                    parent_job_id="batch.post_close",
                    parent_partition=_date.today().isoformat(),
                )
        except Exception:
            recorder = None
        try:
            outputs = svc.workflow.run_once(recorder=recorder) if recorder else svc.workflow.run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("trigger batch.post_close failed")
            raise HTTPException(500, str(exc)[:300]) from exc
        return {
            "status": "ok",
            "portfolio_size": len(outputs.get("portfolio-agent", {}).get("portfolio_size", []) or []) or
                              outputs.get("portfolio-agent", {}).get("portfolio_size", 0),
            "advisor": (outputs.get("advisor-agent") or {}).get("rendered", "")[:400],
        }

    if name == "batch.deep_research":
        from akq_agents.orchestrator.jobs.batch_deep_research import _do

        # 构造 services dict 给 _do
        ws_services = svc.workflow.services if svc.workflow else {}
        try:
            result = _do(ws_services)
        except Exception as exc:  # noqa: BLE001
            logger.exception("trigger batch.deep_research failed")
            raise HTTPException(500, str(exc)[:300]) from exc
        return {"status": "ok", **result}

    if name == "factor.discovery":
        engine = svc.discovery_engine
        if engine is None:
            raise HTTPException(503, "discovery_engine not ready")
        try:
            stats = engine.run_batch(n_candidates=n_candidates, as_of_date=date.today())
        except Exception as exc:  # noqa: BLE001
            logger.exception("trigger factor.discovery failed")
            raise HTTPException(500, str(exc)[:300]) from exc
        return {"status": "ok", **stats.as_dict()}

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
            "target_date": target_date or date.today().isoformat(),
            "result": None,
            "error": None,
        })

    repo = svc.repo
    tdate = date.fromisoformat(target_date) if target_date else date.today()

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
