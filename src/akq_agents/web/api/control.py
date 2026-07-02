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


_SUPPORTED_JOBS = {
    "batch.post_close", "batch.deep_research", "factor.discovery", "factor.eviction",
    # M24: 4 个 user-facing 业务, picker 跑完写 job_results, 前端 GET /result 端点轮询.
    "factor.backtest_single", "factor.brainstorm",
    "portfolio.trade_list_recompute", "portfolio.nav_rebuild",
}

# M24: 这 4 个 job_id 走 picker 时 payload 字段映射, control.trigger 透传给 daemon.
# 其他 8 个 (batch.* / factor.discovery / factor.eviction) 沿用原 payload schema.
_USER_FACING_PAYLOAD_KEYS: dict[str, list[str]] = {
    "factor.backtest_single": ["factor_name", "days", "rebalance_step", "top_n"],
    "factor.brainstorm": ["n"],
    "portfolio.trade_list_recompute": [],
    "portfolio.nav_rebuild": [],
}


def _manual_partition(base: str) -> str:
    """手动触发用的 partition: ``{base}-manual-{6 hex}`` — 永不撞 cron 的同 partition.

    M19: cron 路径用裸 hour/day 桶 + (job_id, partition) UNIQUE 防 misfire 重复触发。
    手动路径加 -manual-{rand} 后缀, 让用户每次点都跑一次新执行 (前端 button.disabled
    防同一次点的 race)。同 day 内手动跑多次, ops 看板能看到独立的 N 行记录。
    """
    import uuid
    return f"{base}-manual-{uuid.uuid4().hex[:6]}"


@router.post("/jobs/{name}/trigger")
async def trigger_job(
    name: str,
    body: dict[str, Any] | None = None,
    n_candidates: int = 20,
    force_full: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """统一手动触发入口。M24: 8 + 4 = 12 个 job 全部走 pending_triggers 异步通道, 立即 202.

    M23: web 永远不跑业务, 写 pending_triggers + 立即 202, daemon picker 5s 扫一次 claim 跑.
    M19: partition = `{base}-manual-{hex6}` 唯一, 多次手动点击各自独立.

    Args (兼容老 query param + M24 body):
    - query 模式: n_candidates (factor.discovery), force_full (batch.deep_research), dry_run (factor.eviction)
    - body 模式 (M24): factor.backtest_single {factor_name, days, rebalance_step, top_n} 等
    - 优先级: body 优先; query 缺省值仅给老 job 用, M24 新 job 完全靠 body.
    """
    if name not in _SUPPORTED_JOBS:
        raise HTTPException(404, f"unknown job: {name}")
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        raise HTTPException(503, "sched_store not ready (web container 未装配)")
    base_partition = date.today().isoformat()
    partition = _manual_partition(base_partition)

    # 并发防护
    if svc.sched_store.has_pending_or_running_for_job(name):
        raise HTTPException(
            409,
            f"{name} 已有 pending/running 任务, 请等当前任务结束再触发 (看 /api/ops/job-runs 状态)",
        )

    # 构造 payload: 老 job 走 query param, M24 新 job 走 body 透传.
    payload: dict[str, Any] = dict(body or {})
    if name == "batch.deep_research":
        payload.setdefault("mode", "full" if force_full else "fast")
    elif name == "factor.discovery":
        payload.setdefault("n_candidates", n_candidates)
    elif name == "factor.eviction":
        payload.setdefault("dry_run", dry_run)
    elif name in _USER_FACING_PAYLOAD_KEYS:
        # M24: 业务参数 (factor_name / days / n 等) 必须在 body 传, 缺关键字段 400.
        if name == "factor.backtest_single" and not payload.get("factor_name"):
            raise HTTPException(400, "factor.backtest_single 需要 body.factor_name")
        # 截白名单外的字段 (前端不小心多塞字段也不会被 daemon 误用)
        allowed = set(_USER_FACING_PAYLOAD_KEYS[name])
        payload = {k: v for k, v in payload.items() if k in allowed}

    trigger_id = svc.sched_store.create_pending_trigger(
        job_id=name, partition=partition, payload=payload,
    )

    # 立即写一行 job_runs.status='pending' 让 UI /api/ops/job-runs 立刻能看到记录.
    try:
        svc.sched_store.upsert_job_run(
            job_id=name, partition=partition, status="pending",
            payload={"trigger_id": trigger_id, **(payload or {})},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert job_runs pending failed (non-fatal): %s", exc)

    # M24: user-facing job 给前端多返一个 result_poll_url 端点, 业务跑完读 result.
    result_poll_url = None
    if name in _USER_FACING_PAYLOAD_KEYS:
        result_poll_url = f"/api/control/jobs/{name}/{partition}/result"

    return {
        "status": "accepted",
        "reason_code": "ASYNC_QUEUED",
        "payload": {
            "job_id": name,
            "partition": partition,
            "trigger_id": trigger_id,
            "poll_url": f"/api/ops/job-runs/{name}/{partition}/detail",
            "result_poll_url": result_poll_url,
            "hint": "任务已入队, daemon 5s 内 pick 起来跑, 进度查 poll_url, M24 user-facing job 跑完结果查 result_poll_url",
        },
    }


@router.get("/jobs/{name}/{partition}/result")
async def get_job_result(name: str, partition: str) -> dict[str, Any]:
    """M24: 前端轮询读 user-facing job 的业务结果.

    返回:
    - 200 + result JSON: picker 写好了
    - 200 + {status: "running"}: trigger 存在, picker 还没写 result (前端继续 poll)
    - 200 + {status: "not_found"}: 没 trigger 过 (前端报 404, 让用户重 trigger)
    - 503: sched_store 没装
    """
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        raise HTTPException(503, "sched_store not ready")
    if name not in _SUPPORTED_JOBS:
        raise HTTPException(404, f"unknown job: {name}")
    # 先看 trigger 行存不存在 (写 pending_triggers 时落地)
    # 没直接接口, 但 job_runs 一定有 pending/ok/failed 行 — 用它判定 "not_found" vs "running"
    jr = svc.sched_store.get_job_run(name, partition)
    if jr is None:
        return {"status": "not_found", "job_id": name, "partition": partition}
    result = svc.sched_store.get_job_result(name, partition)
    if result is None:
        # trigger 存在但 result_json 还是 NULL → 业务还在跑
        return {
            "status": "running",
            "job_id": name,
            "partition": partition,
            "job_run_status": jr.status,
        }
    return {
        "status": "ok",
        "job_id": name,
        "partition": partition,
        "result": result,
    }


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
