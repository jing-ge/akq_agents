"""Ops endpoints：/api/ops/health|job-runs|events。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    """聚合：DataHealth + DaemonState + today_batch + scheduler_events_24h_by_level。"""
    svc: ServiceContainer = get_services()
    data_health = None
    if svc.repo is not None:
        try:
            data_health = svc.repo.quality_report().model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            data_health = {"error": str(exc)[:200]}

    daemon: dict[str, Any] = {"state": None, "is_alive": False}
    if svc.daemon_state_file is not None:
        state = svc.daemon_state_file.read()
        daemon = {
            "state": state.to_dict() if state else None,
            "is_alive": svc.daemon_state_file.is_alive(max_age_s=600),
        }

    today_batch: dict[str, Any] | None = None
    if svc.sched_store is not None:
        from datetime import date as _date

        run = svc.sched_store.get_job_run("batch.post_close", _date.today().isoformat())
        if run is not None:
            today_batch = {
                "status": run.status,
                "reason_code": run.reason_code,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_ms": run.duration_ms,
            }

    scheduler_events_24h_by_level: dict[str, int] = {"info": 0, "warning": 0, "error": 0}
    if svc.sched_store is not None:
        scheduler_events_24h_by_level = svc.sched_store.events_count_24h_by_level()

    return {
        "data_health": data_health,
        "daemon": daemon,
        "today_batch": today_batch,
        "scheduler_events_24h_by_level": scheduler_events_24h_by_level,
    }


@router.get("/job-runs")
async def job_runs(
    limit: int = Query(default=50, ge=1, le=500),
    job_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"runs": [], "n": 0}
    runs = svc.sched_store.list_recent_runs(limit=limit, job_id=job_id, status=status)
    return {
        "runs": [
            {
                "id": r.id,
                "job_id": r.job_id,
                "partition": r.partition,
                "status": r.status,
                "reason_code": r.reason_code,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration_ms": r.duration_ms,
                "payload": json.loads(r.payload_json) if r.payload_json else None,
            }
            for r in runs
        ],
        "n": len(runs),
    }


@router.get("/events")
async def events(
    limit: int = Query(default=50, ge=1, le=500),
    level_min: str | None = Query(default=None),
    kind_prefix: str | None = Query(default=None),
) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"events": [], "n": 0}
    rows = svc.sched_store.list_events(limit=limit, level_min=level_min, kind_prefix=kind_prefix)
    return {
        "events": [
            {
                "id": e.id,
                "ts": e.ts,
                "level": e.level,
                "kind": e.kind,
                "source": e.source,
                "payload": json.loads(e.payload_json) if e.payload_json else None,
            }
            for e in rows
        ],
        "n": len(rows),
    }


# ============================================================
# M11: 任务详情 + 子步骤 + 日志
# ============================================================


@router.get("/job-runs/{job_id}/{partition}/detail")
async def job_run_detail(job_id: str, partition: str) -> dict[str, Any]:
    """某 job 某 partition 的详细执行轨迹（含 job_steps 子步骤）。"""
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"error": "no_state_store"}
    run = svc.sched_store.get_job_run(job_id, partition)
    if run is None:
        return {"error": "not_found", "job_id": job_id, "partition": partition}

    # 读 job_steps
    steps = []
    if svc.repo is not None:
        from akq_agents.orchestrator.step_recorder import StepReader

        reader = StepReader(svc.repo._base_dir / "meta.db")
        steps = reader.list_steps(job_id, partition)

    return {
        "job_id": job_id,
        "partition": partition,
        "status": run.status,
        "reason_code": run.reason_code,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "payload": json.loads(run.payload_json) if run.payload_json else None,
        "steps": steps,
        "n_steps": len(steps),
    }


@router.get("/logs")
async def get_logs(
    source: str = Query(default="daemon", regex="^(daemon|web|akquant_backtest)$"),
    lines: int = Query(default=200, ge=10, le=2000),
    grep: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    """读物理日志文件尾部 N 行，可选 grep 过滤。"""
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[4]
    log_path = {
        "daemon": project_root / "data" / "daemon.log",
        "web": project_root / "data" / "web.log",
        "akquant_backtest": project_root / "data" / "bootstrap.log",
    }.get(source)
    if log_path is None or not log_path.exists():
        return {"source": source, "lines": [], "exists": False}

    try:
        # 简单 tail：读最后 ~200KB 然后切行
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(file_size, 200 * 1024)
            f.seek(file_size - read_size)
            chunk = f.read().decode("utf-8", errors="replace")
        all_lines = chunk.splitlines()
    except Exception as exc:  # noqa: BLE001
        return {"source": source, "error": str(exc)[:200], "lines": []}

    if grep:
        all_lines = [ln for ln in all_lines if grep.lower() in ln.lower()]
    tail = all_lines[-lines:]
    return {
        "source": source,
        "path": str(log_path),
        "file_size": file_size,
        "total_lines_returned": len(tail),
        "lines": tail,
        "exists": True,
    }
