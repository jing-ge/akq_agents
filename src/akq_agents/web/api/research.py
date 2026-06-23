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
        decay_verdict = None  # P1-4 衰减判定
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
            # P1-4: 取 30 天历史算衰减
            try:
                history = svc.factor_evaluator.list_history(f.name, limit=30)
                irs = [float(m.ir) for m in history if m.ir is not None]
                if len(irs) >= 6:
                    mid = len(irs) // 2
                    ir_recent = sum(abs(x) for x in irs[:mid]) / mid
                    ir_earlier = sum(abs(x) for x in irs[mid:]) / max(len(irs) - mid, 1)
                    ir_peak = max(abs(x) for x in irs)
                    ir_now = abs(irs[0])
                    if ir_earlier > 0.1 and ir_recent < 0.6 * ir_earlier:
                        decay_verdict = {"level": "severe", "label": "⚠️ 显著衰减",
                                        "ir_recent": ir_recent, "ir_earlier": ir_earlier}
                    elif ir_earlier > 0.1 and ir_recent < 0.8 * ir_earlier:
                        decay_verdict = {"level": "mild", "label": "轻微衰减",
                                        "ir_recent": ir_recent, "ir_earlier": ir_earlier}
                    elif ir_now < 0.6 * ir_peak and ir_peak > 0.2:
                        decay_verdict = {"level": "off_peak", "label": "已离峰值",
                                        "ir_now": ir_now, "ir_peak": ir_peak}
            except Exception:
                pass
        rows.append(
            {
                "name": f.name,
                "factor_version": f.factor_version,
                "direction": f.direction,
                "lookback_days": f.lookback_days,
                "last_metric": latest,
                "decay": decay_verdict,
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


# ============================================================
# M14: LLM 因子构建方向（brainstorm）
# ============================================================


@router.get("/factors/llm-suggestions")
async def llm_suggestions_list(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """列出 status='llm_suggested' 的待审核提议。"""
    svc: ServiceContainer = get_services()
    store = _proposal_store(svc)
    if store is None:
        return {"suggestions": [], "n": 0}
    rows = store.list_recent(limit=limit, status="llm_suggested")
    return {
        "suggestions": [
            {
                "factor_name": r.factor_name,
                "recipe": json.loads(r.recipe_json),
                "direction": r.direction,
                "reason": r.reason,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "n": len(rows),
    }


@router.post("/factors/brainstorm/run")
async def trigger_brainstorm(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """手动触发一次 LLM brainstorm（同步等结果）。"""
    svc: ServiceContainer = get_services()
    if svc.workflow is None:
        raise HTTPException(503, detail="workflow not ready")
    services = svc.workflow.services
    if "llm_factor_brainstormer" not in services:
        raise HTTPException(503, detail="llm_factor_brainstormer not configured (检查 LLM 是否启用)")
    if "job_runner" not in services:
        raise HTTPException(503, detail="job_runner not available")
    n = int((payload or {}).get("n", 10))
    n = max(1, min(n, 30))

    from akq_agents.orchestrator.jobs.factor_brainstorm import run_once_now

    out = run_once_now(services["job_runner"], services, n=n)
    return {"ok": True, "result": str(out)}


@router.post("/factors/llm-suggestions/{factor_name}/{action}")
async def review_llm_suggestion(factor_name: str, action: str) -> dict[str, Any]:
    """人工审核 LLM 提议：accept → status='shadow'，reject → status='rejected'。"""
    if action not in ("accept", "reject"):
        raise HTTPException(400, detail=f"action must be accept|reject, got {action!r}")
    svc: ServiceContainer = get_services()
    store = _proposal_store(svc)
    if store is None:
        raise HTTPException(503, detail="proposal_store not available")
    if svc.repo is None:
        raise HTTPException(503, detail="repo not available")

    # 直接走 meta.db 改 status（FactorProposalStore.upsert 会要求重新填全字段，太啰嗦；这里 SQL 改 status 列）
    from akq_agents.services.data.repository import open_meta_db
    from akq_agents.services.factors.proposal_store import now_iso

    db_path = svc.repo._base_dir / "meta.db"
    with open_meta_db(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM factor_proposals WHERE factor_name = ?",
            (factor_name,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"factor not found: {factor_name}")
        if row[0] != "llm_suggested":
            raise HTTPException(409, detail=f"factor status is {row[0]!r}, not 'llm_suggested'")

        new_status = "shadow" if action == "accept" else "rejected"
        ts = now_iso()
        if action == "accept":
            conn.execute(
                "UPDATE factor_proposals SET status=?, shadow_started_at=?, evaluated_at=? "
                "WHERE factor_name=?",
                (new_status, ts, ts, factor_name),
            )
        else:
            conn.execute(
                "UPDATE factor_proposals SET status=?, evaluated_at=? WHERE factor_name=?",
                (new_status, ts, factor_name),
            )
        conn.commit()
    return {"ok": True, "factor_name": factor_name, "status": new_status}


def _proposal_store(svc: ServiceContainer):
    """优先用顶层字段 svc.proposal_store；fallback 到 svc.workflow.services["factor_proposal_store"]。"""
    store = getattr(svc, "proposal_store", None)
    if store is not None:
        return store
    if svc.workflow is not None:
        return svc.workflow.services.get("factor_proposal_store")
    return None
