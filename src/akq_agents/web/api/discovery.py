"""Discovery endpoints：/api/research/proposals + /api/research/discovery-stats。

为 Research 页提供 M2 自动因子发现的可视化数据。
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
