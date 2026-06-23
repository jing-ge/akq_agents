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
    """手动触发一次 LLM brainstorm（同步等结果）。

    C5: web 进程在 deps.py 装了独立 JobRunner（与 daemon 共用 sched_store +
    meta.db UNIQUE 约束）。走 JobRunner 写 job_runs/events，与 daemon 20:00
    cron 一致；UI ops 看板能看到操作记录。
    """
    from datetime import date as _date

    svc: ServiceContainer = get_services()
    if svc.workflow is None:
        raise HTTPException(503, detail="workflow not ready")
    if svc.job_runner is None:
        raise HTTPException(503, detail="job_runner not ready")
    services = svc.workflow.services
    brainstormer = services.get("llm_factor_brainstormer")
    if brainstormer is None:
        raise HTTPException(503, detail="llm_factor_brainstormer not configured (检查 LLM 是否启用)")
    n = int((payload or {}).get("n", 10))
    n = max(1, min(n, 30))

    from akq_agents.orchestrator.jobs.factor_brainstorm import JOB_ID

    def _do_brainstorm() -> dict[str, Any]:
        return brainstormer.run(n=n)

    result = svc.job_runner.run(JOB_ID, _date.today().isoformat(), _do_brainstorm, timeout_s=120)
    return {
        "ok": result.status == "ok",
        "status": result.status,
        "reason_code": result.reason_code,
        "stats": result.payload,
    }


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


@router.get("/factors/shadow-stats")
async def shadow_stats() -> dict[str, Any]:
    """Shadow 因子战况：每个 shadow 已观察天数、当前 OOS IR、判定。

    判定规则（与 DiscoveryThresholds 对齐）:
    - evaluating: oos_observations < 20 (评估中)
    - no_data: oos_observations >= 20 但 oos_ir is None
    - promote_eligible: |oos_ir| >= 0.15
    - should_demote: oos_observations >= 60 且 |oos_ir| < 0.10
    - edge: 中间地带（继续观察）
    """
    from datetime import datetime as _dt

    svc: ServiceContainer = get_services()
    store = svc.proposal_store
    if store is None:
        return {"shadows": [], "n": 0}
    rows = store.list_shadow()
    now = _dt.now()
    out = []
    for r in rows:
        days_observed = None
        if r.shadow_started_at:
            try:
                shadow_d = _dt.fromisoformat(r.shadow_started_at)
                days_observed = (now - shadow_d).days
            except Exception:  # noqa: BLE001
                days_observed = None
        out.append({
            "factor_name": r.factor_name,
            "direction": r.direction,
            "shadow_started_at": r.shadow_started_at,
            "days_observed": days_observed,
            "oos_observations": r.oos_observations,
            "oos_ir": r.oos_ir,
            "ir": r.ir,
            "is_llm": r.factor_name.startswith("llm_"),
            "verdict": _shadow_verdict(r.oos_observations, r.oos_ir),
        })
    return {"shadows": out, "n": len(out)}


def _shadow_verdict(oos_obs, oos_ir):
    """与 DiscoveryThresholds 阈值对齐 (shadow_min_oos_days=20, shadow_min_oos_ir=0.15,
    shadow_max_days=60, shadow_min_keep_ir=0.10)。"""
    if oos_obs is None or oos_obs < 20:
        return "evaluating"
    if oos_ir is None:
        return "no_data"
    if abs(oos_ir) >= 0.15:
        return "promote_eligible"
    if oos_obs >= 60 and abs(oos_ir) < 0.10:
        return "should_demote"
    return "edge"


@router.get("/daily-attribution")
async def daily_attribution(date: str = Query(..., description="YYYY-MM-DD")) -> dict[str, Any]:
    """当日组合 PnL 分解：top 5 涨/跌票 + 因子贡献排名。

    数据源:
    - portfolio_snapshots (当日 weight + top_factors_json)
    - ohlcv parquet (今日 close + 前一交易日 close → 个股日收益)

    返回字段:
    - top_contributors: 涨幅 top 5 票（按 prev_weight × ret 排序）
    - top_draggers: 跌幅 top 5 票
    - factor_contribution: 按 top_factors_json 聚合的因子贡献排名 (top 8)
    """
    svc: ServiceContainer = get_services()
    if svc.portfolio_store is None or svc.repo is None:
        raise HTTPException(503, "stores not ready")
    try:
        d = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, f"invalid date: {date!r}")  # noqa: B904

    rows = svc.portfolio_store.read_snapshot(d)
    if not rows:
        raise HTTPException(404, {"error": "no_snapshot", "date": date})

    today_close, prev_close = _load_close_pair(svc.repo, [r.symbol for r in rows], d)

    contribs = []
    for r in rows:
        c_t = today_close.get(r.symbol)
        c_p = prev_close.get(r.symbol)
        prev_w = float(r.prev_weight or 0.0)
        if c_t is None or c_p is None or c_p <= 0 or prev_w <= 0:
            continue
        ret = c_t / c_p - 1
        bps = ret * prev_w * 10000
        contribs.append(
            {
                "symbol": r.symbol,
                "name": r.name,
                "industry": r.industry,
                "prev_weight": prev_w,
                "ret_pct": ret,
                "contrib_bps": bps,
            }
        )
    contribs.sort(key=lambda x: x["contrib_bps"], reverse=True)

    factor_total: dict[str, float] = {}
    for r in rows:
        if not r.top_factors_json:
            continue
        try:
            top_factors = json.loads(r.top_factors_json)
        except Exception:  # noqa: BLE001
            continue
        for f in top_factors:
            name = f.get("name")
            c = f.get("contribution", 0.0)
            if name is None or c is None:
                continue
            factor_total[name] = factor_total.get(name, 0.0) + float(c)
    factor_rank = sorted(factor_total.items(), key=lambda kv: abs(kv[1]), reverse=True)[:8]

    return {
        "date": date,
        "n_holdings": len(rows),
        "n_with_return": len(contribs),
        "top_contributors": contribs[:5],
        "top_draggers": list(reversed(contribs[-5:])),
        "factor_contribution": [{"name": n, "contribution": v} for n, v in factor_rank],
    }


def _load_close_pair(repo, symbols, d):
    """从 ohlcv parquet 拉 (today_close, prev_trading_day_close) 两个 dict。"""
    from datetime import timedelta as _td

    import pyarrow.dataset as ds

    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    if ohlcv_dir is None or not ohlcv_dir.exists():
        return {}, {}
    start = (d - _td(days=10)).isoformat()
    end = d.isoformat()
    dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
    table = dataset.to_table(
        filter=(ds.field("date") >= start)
        & (ds.field("date") <= end)
        & ds.field("symbol").isin(list(symbols)),
        columns=["date", "symbol", "close"],
    )
    df = table.to_pandas()
    if df.empty:
        return {}, {}
    df["date"] = df["date"].astype(str)
    today_str = d.isoformat()
    today_df = df[df["date"] == today_str]
    today = {str(r["symbol"]): float(r["close"]) for _, r in today_df.iterrows()}
    prev_df = df[df["date"] < today_str]
    if prev_df.empty:
        return today, {}
    latest_prev = prev_df["date"].max()
    prev_rows = prev_df[prev_df["date"] == latest_prev]
    prev = {str(r["symbol"]): float(r["close"]) for _, r in prev_rows.iterrows()}
    return today, prev


@router.get("/factors/correlation")
async def factors_correlation(
    date: str | None = Query(None, description="YYYY-MM-DD，缺省用最近一日"),
    lookback_days: int = Query(60, ge=20, le=180),
) -> dict[str, Any]:
    """返回 active 因子两两 cross-section 相关性矩阵。"""
    from datetime import timedelta as _td

    svc: ServiceContainer = get_services()
    if svc.repo is None or svc.factor_registry is None:
        raise HTTPException(503, "factor_registry or repo not ready")

    factor_engine = svc.workflow.services.get("factor_engine") if svc.workflow else None
    if factor_engine is None:
        raise HTTPException(503, "factor_engine not available")

    factors = svc.factor_registry.list_all()
    if not factors:
        return {"factors": [], "matrix": [], "n_observations": 0}

    target_d = _date.fromisoformat(date) if date else _date.today()
    max_lookback = max((f.lookback_days for f in factors), default=60)
    start = target_d - _td(days=max(lookback_days, max_lookback * 2))

    try:
        universe = svc.repo.get_universe(target_d)
    except Exception:  # noqa: BLE001
        universe = None

    symbols = list(universe.symbols) if universe is not None else []
    if symbols:
        ohlcv = svc.repo.get_ohlcv_loose(symbols, start, target_d)
    else:
        ohlcv = svc.repo.get_ohlcv_loose([], start, target_d)
    if ohlcv.empty:
        raise HTTPException(404, {"error": "no_ohlcv_data", "date": target_d.isoformat()})

    df = factor_engine.compute(ohlcv, factors)
    if df.empty:
        return {"factors": [], "matrix": [], "n_observations": 0}

    corr = df.corr()
    factor_names = list(corr.columns)
    matrix = corr.values.tolist()

    return {
        "as_of_date": target_d.isoformat(),
        "factors": factor_names,
        "matrix": matrix,
        "n_observations": int(df.dropna(how="any").shape[0]),
        "n_total_symbols": int(df.shape[0]),
    }
