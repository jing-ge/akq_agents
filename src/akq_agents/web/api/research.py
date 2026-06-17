"""Research endpoints：/api/research/portfolio* + /factors*。"""

from __future__ import annotations

import json
from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


# ---------------- Portfolio ----------------


@router.get("/portfolio")
async def portfolio(date: str = Query(..., description="YYYY-MM-DD")) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.portfolio_store is None:
        raise HTTPException(503, detail="portfolio store not available")
    try:
        d = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, detail=f"invalid date {date!r}")  # noqa: B904
    rows = svc.portfolio_store.read_snapshot(d)
    if not rows:
        raise HTTPException(404, detail={"error": "no_snapshot_for_date", "date": date})

    industry_totals: dict[str, float] = {}
    out_rows = []
    for r in rows:
        ind = r.industry or "未分类"
        industry_totals[ind] = industry_totals.get(ind, 0.0) + float(r.weight)
        out_rows.append(
            {
                "symbol": r.symbol,
                "name": r.name,
                "industry": r.industry,
                "weight": r.weight,
                "prev_weight": r.prev_weight,
                "composite_score": r.composite_score,
                "top_factors": json.loads(r.top_factors_json or "[]"),
            }
        )
    turnover = _compute_turnover_from_rows(rows)
    return {
        "as_of_date": date,
        "n": len(rows),
        "rows": out_rows,
        "industry_breakdown": [{"industry": k, "total_weight": v} for k, v in industry_totals.items()],
        "turnover": turnover,
        "summary": f"持仓 {len(rows)} 只，turnover {turnover * 100:.1f}%",
    }


@router.get("/portfolio/attribution")
async def portfolio_attribution(date: str = Query(..., description="YYYY-MM-DD")) -> dict[str, Any]:
    """从 portfolio_snapshots 聚合 portfolio_contribution。"""
    svc: ServiceContainer = get_services()
    if svc.portfolio_store is None:
        raise HTTPException(503, detail="portfolio store not available")
    try:
        d = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, detail=f"invalid date {date!r}")  # noqa: B904
    rows = svc.portfolio_store.read_snapshot(d)
    if not rows:
        raise HTTPException(404, detail={"error": "no_snapshot_for_date", "date": date})
    factor_contrib: dict[str, float] = {}
    for r in rows:
        per_stock = json.loads(r.top_factors_json or "[]")
        for item in per_stock:
            name = item.get("name", "")
            contrib = float(item.get("contribution", 0.0)) * float(r.weight)
            factor_contrib[name] = factor_contrib.get(name, 0.0) + contrib
    sorted_items = sorted(factor_contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)
    return {
        "as_of_date": date,
        "portfolio_contribution": dict(sorted_items),
        "n_factors": len(sorted_items),
    }


def _compute_turnover_from_rows(rows: list) -> float:
    total = 0.0
    for r in rows:
        prev = float(r.prev_weight or 0.0)
        total += abs(float(r.weight) - prev)
    return total / 2.0


# ---------------- Factors ----------------


@router.get("/factors")
async def factors_list() -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.factor_registry is None:
        return {"factors": [], "n": 0}
    rows = []
    for f in svc.factor_registry.list_all():
        latest = None
        if svc.factor_evaluator is not None:
            m = svc.factor_evaluator.get_latest(f.name, f.factor_version)
            if m is not None:
                latest = {
                    "as_of_date": m.as_of_date,
                    "window_days": m.window_days,
                    "ic_mean": m.ic_mean,
                    "ir": m.ir,
                    "status": m.status,
                }
        rows.append(
            {
                "name": f.name,
                "factor_version": f.factor_version,
                "direction": f.direction,
                "lookback_days": f.lookback_days,
                "last_metric": latest,
            }
        )
    return {"factors": rows, "n": len(rows)}


@router.get("/factors/{name}/metrics")
async def factor_metrics(name: str, limit: int = Query(default=120, ge=1, le=500)) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.factor_evaluator is None:
        return {"name": name, "metrics": [], "n": 0}
    metrics = svc.factor_evaluator.list_history(name, limit=limit)
    return {
        "name": name,
        "metrics": [
            {
                "factor_version": m.factor_version,
                "as_of_date": m.as_of_date,
                "window_days": m.window_days,
                "ic_mean": m.ic_mean,
                "ic_std": m.ic_std,
                "ir": m.ir,
                "t_stat": m.t_stat,
                "status": m.status,
                "reason": m.reason,
            }
            for m in metrics
        ],
        "n": len(metrics),
    }
