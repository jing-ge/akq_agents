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


_SUPPORTED_JOBS = {"batch.post_close", "batch.deep_research", "factor.discovery", "factor.eviction"}


def _manual_partition(base: str) -> str:
    """手动触发用的 partition: ``{base}-manual-{6 hex}`` — 永不撞 cron 的同 partition.

    M19: cron 路径用裸 hour/day 桶 + (job_id, partition) UNIQUE 防 misfire 重复触发。
    手动路径加 -manual-{rand} 后缀, 让用户每次点都跑一次新执行 (前端 button.disabled
    防同一次点的 race)。同 day 内手动跑多次, ops 看板能看到独立的 N 行记录。
    """
    import uuid
    return f"{base}-manual-{uuid.uuid4().hex[:6]}"


@router.post("/jobs/{name}/trigger")
async def trigger_job(name: str, n_candidates: int = 20, force_full: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """同步触发 job。C5: 走 JobRunner 写 job_runs/events，与 daemon cron 路径一致。

    M19: partition 改成 `{base}-manual-{hex6}` 唯一格式, **手动触发不走幂等**, 用户
    点多少次跑多少次。cron 路径仍用裸 hour/day 桶, (job_id, partition) UNIQUE 防 misfire
    重复触发的语义不变。前端 button.disabled 防连点导致的 race。

    Args:
        force_full: 仅对 batch.deep_research 生效. False (默认, fast 模式) — 只算 db
            缺失的日期, 通常几分钟跑完. True (full 模式) — 重算全部 90 天历史 IC 覆盖
            db, ~10-15 分钟. 数据修复 / 怀疑历史错算时用。
        dry_run: 仅对 factor.eviction 生效. True — 只统计待淘汰名单不真删, 用户先看准了再执行。
    """
    if name not in _SUPPORTED_JOBS:
        raise HTTPException(404, f"unknown job: {name}")
    svc: ServiceContainer = get_services()
    if svc.job_runner is None:
        raise HTTPException(503, "job_runner not ready (web container 未装配)")
    base_partition = date.today().isoformat()
    partition = _manual_partition(base_partition)

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
        # bootstrap.py:303 里 daemon_services 除了 workflow.services 还额外注入了
        # workflow 自身；手动 trigger 路径要与 daemon 对齐，否则 _do 里
        # services["workflow"] 会 KeyError。
        ws_services["workflow"] = svc.workflow
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
        mode = "full" if force_full else "fast"
        result = svc.job_runner.run(JOB_ID, partition, lambda: _do(ws_services, mode=mode), timeout_s=5400)
        return {
            "status": result.status,
            "reason_code": result.reason_code,
            "payload": result.payload,
        }

    if name == "factor.discovery":
        engine = svc.discovery_engine
        if engine is None:
            raise HTTPException(503, "discovery_engine not ready")
        from akq_agents.orchestrator.jobs.factor_discovery import JOB_ID, _partition_for_now

        def _do_discovery() -> dict[str, Any]:
            stats = engine.run_batch(n_candidates=n_candidates, as_of_date=date.today())
            return stats.as_dict()

        # 手动 partition 用 hour 桶 + manual 后缀, 跟 daemon cron hour 桶分开命名空间
        discovery_partition = _manual_partition(_partition_for_now())
        result = svc.job_runner.run(JOB_ID, discovery_partition, _do_discovery, timeout_s=900)
        return {
            "status": result.status,
            "reason_code": result.reason_code,
            "payload": result.payload,
        }

    if name == "factor.eviction":
        from akq_agents.orchestrator.jobs.factor_eviction import _do as _do_eviction
        from akq_agents.orchestrator.jobs.factor_eviction import JOB_ID
        from akq_agents.bootstrap import load_scheduler_config

        scheduler_cfg = load_scheduler_config()
        ws_services = svc.workflow.services if svc.workflow else {}

        def _do_evict() -> dict[str, Any]:
            return _do_eviction(ws_services, scheduler_cfg, dry_run=dry_run)

        result = svc.job_runner.run(JOB_ID, partition, _do_evict, timeout_s=300)
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
