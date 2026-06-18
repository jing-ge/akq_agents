"""Control endpoints：从 Web 控制台操控后台。

提供：
- ``POST /daemon/start`` — 后台启动 daemon（subprocess + pid 文件）
- ``POST /daemon/stop`` — 给 daemon pid 发 SIGTERM
- ``POST /jobs/{name}/trigger`` — 同步触发某个 job

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
