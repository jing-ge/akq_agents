"""Discovery + NAV endpoints。

为 Research 页提供 M2 自动因子发现的可视化数据 + M7-A 组合净值曲线。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


@router.get("/proposals")
async def list_proposals(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出因子提案流水（最近 N 条 + 计数）。"""
    svc: ServiceContainer = get_services()
    if svc.proposal_store is None:
        return {"counts": {}, "rows": []}
    rows = svc.proposal_store.list_recent(limit=limit, status=status)
    out = [
        {
            "factor_name": r.factor_name,
            "status": r.status,
            "ir": r.ir,
            "ic_mean": r.ic_mean,
            "t_stat": r.t_stat,
            "max_abs_corr": r.max_abs_corr,
            "reason": r.reason,
            "recipe": json.loads(r.recipe_json),
            "evaluated_at": r.evaluated_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return {"counts": svc.proposal_store.counts(), "rows": out, "n": len(out)}


@router.get("/nav")
async def get_nav() -> dict[str, Any]:
    """读取组合净值曲线（扣费后） + benchmark 对比 + 汇总指标。"""
    svc: ServiceContainer = get_services()
    workflow = svc.workflow
    backtester = workflow.services.get("portfolio_backtester") if workflow else None
    if backtester is None:
        return {"nav": [], "summary": {}}
    df = backtester.read_nav()
    if df.empty:
        return {"nav": [], "summary": {"reason": "no_data; 先用 scripts/backfill_portfolio_history.py 跑出历史 snapshot"}}
    nav_list = [
        {
            "date": str(r["as_of_date"]),
            "nav_net": float(r["nav_net"]),
            "nav_gross": float(r["nav_gross"]) if r["nav_gross"] is not None else None,
            "benchmark_nav": float(r["benchmark_nav"]) if r["benchmark_nav"] is not None else None,
            "turnover": float(r["turnover"]) if r["turnover"] is not None else None,
        }
        for _, r in df.iterrows()
    ]
    # 汇总（直接调一次 backtester 在不重算的情况下也能算）
    import pandas as pd

    summary = backtester._summarize(df)
    return {"nav": nav_list, "summary": summary, "n": len(nav_list)}


@router.post("/nav/rebuild")
async def rebuild_nav() -> dict[str, Any]:
    """手动触发 NAV 重新计算。"""
    svc: ServiceContainer = get_services()
    workflow = svc.workflow
    backtester = workflow.services.get("portfolio_backtester") if workflow else None
    if backtester is None:
        return {"status": "no_backtester"}
    result = backtester.rebuild_full_history()
    return {"status": "ok", "summary": result.summary, "n_days": len(result.nav)}
