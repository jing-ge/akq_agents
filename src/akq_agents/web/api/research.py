"""Research endpoints：/api/research/portfolio* + /factors*。"""

from __future__ import annotations

import json
from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


def _backfill_names(svc: Any, items: list[dict[str, Any]]) -> None:
    """对每个 item 兜底 name：若 item['name'] 空，从 stock_name_store 查一下补上。

    用于 portfolio_snapshots 等历史表 name 列为空的场景（P1 期 name_map={} 没填）。

    svc 只需暴露 ``svc.workflow.services``（ServiceContainer 或鸭子类型替身均可）。
    """
    if not items:
        return
    workflow = svc.workflow
    name_store = workflow.services.get("stock_name_store") if workflow else None
    if name_store is None:
        return
    # 只在至少一个 name 缺失时才查 store，避免热路径不必要 IO
    if all(it.get("name") for it in items):
        return
    name_map = name_store.load_all()
    if not name_map:
        return
    for it in items:
        if not it.get("name"):
            it["name"] = name_map.get(str(it.get("symbol")), "")


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
    _backfill_names(svc, out_rows)
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
    """因子全集列表 (M19): 不只是 registry.list_all() 的 builtin+accepted, 还 union
    factor_proposals 里 shadow / 历史 rejected / demoted (跳过 compute_error).

    每行额外返回:
    - status: builtin | accepted | shadow | rejected | demoted (UI 区分)
    - composite_weight: 该因子在最近一次 IR-EWMA 加权下的权重 (None=未参与组合)
    - selected_top50: 该因子是否被最近一日 portfolio_snapshots top50 任一股的 top_factors 提到
    - oos_observations / oos_ir: shadow 因子专属字段 (registry 因子留 None)

    用户需求: "看到每个因子每天的 ICIR 以及有没有入选权重".
    """
    svc: ServiceContainer = get_services()
    if svc.factor_registry is None:
        return {"factors": [], "n": 0}

    # 1) 收集 registry 里的 active 因子 (builtin + accepted 已 promoted)
    registry_factors = {f.name: f for f in svc.factor_registry.list_all()}

    # 2) 收集 proposal_store 里的 status (含 status='accepted' 用于打标签, shadow, 历史 rejected/demoted)
    proposal_rows: dict[str, dict[str, Any]] = {}
    if svc.proposal_store is not None and svc.repo is not None:
        try:
            from akq_agents.services.data.repository import open_meta_db
            db_path = svc.repo._base_dir / "meta.db"
            with open_meta_db(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT factor_name, status, reason, oos_observations, oos_ir,
                           ic_mean, ir, shadow_started_at, evaluated_at
                    FROM factor_proposals
                    WHERE status IN ('accepted', 'shadow', 'rejected', 'demoted')
                    """
                ).fetchall()
            for name, status, reason, oos_obs, oos_ir, p_ic, p_ir, shadow_at, eval_at in rows:
                # 跳过 compute_error 类 rejected (recipe 死的, 评估也是 NULL)
                if status == "rejected" and reason and reason.startswith("compute_error"):
                    continue
                proposal_rows[name] = {
                    "p_status": status,
                    "p_reason": reason,
                    "oos_observations": oos_obs,
                    "oos_ir": oos_ir,
                    "p_ic_mean": p_ic,
                    "p_ir": p_ir,
                    "shadow_started_at": shadow_at,
                    "evaluated_at": eval_at,
                }
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning("factors_list: read proposals failed: %s", exc)

    # 3) 算 composite_weight (只对 registry 里的因子有意义, shadow/rejected 不参与组合, 留 None)
    composite_weights: dict[str, float] = {}
    if svc.composite_scorer is not None and registry_factors:
        try:
            weights = svc.composite_scorer.compute_weights_for(list(registry_factors.keys()))
            composite_weights = {name: float(w) for name, w in weights.items()}
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning("factors_list: compute_weights failed: %s", exc)

    # 4) 收集"被最近一日 portfolio top 50 任一股 top_factors_json 提到的因子" → selected_top50
    selected_factor_names: set[str] = set()
    latest_snapshot_date: str | None = None
    if svc.portfolio_store is not None and svc.repo is not None:
        try:
            dates = svc.portfolio_store.list_dates(limit=1)
            if dates:
                latest_snapshot_date = dates[0]
                from akq_agents.services.data.repository import open_meta_db
                db_path = svc.repo._base_dir / "meta.db"
                with open_meta_db(db_path) as conn:
                    rows = conn.execute(
                        "SELECT top_factors_json FROM portfolio_snapshots WHERE as_of_date=?",
                        (latest_snapshot_date,),
                    ).fetchall()
                for (raw,) in rows:
                    if not raw:
                        continue
                    try:
                        for it in json.loads(raw):
                            n = it.get("name")
                            if n:
                                selected_factor_names.add(n)
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning("factors_list: read top_factors failed: %s", exc)

    # 5) builtin 命名前缀, 用于区分 builtin / accepted
    builtin_prefixes = ("momentum_", "reversal_", "volatility_", "amount_", "log_amount_")

    def _classify(name: str, in_registry: bool, p_row: dict[str, Any] | None) -> str:
        if any(name.startswith(p) for p in builtin_prefixes):
            return "builtin"
        if p_row is None:
            return "accepted" if in_registry else "unknown"
        return p_row["p_status"]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 5.a) 先遍历 registry (active 因子, 有完整 Factor 实例信息)
    for name, f in registry_factors.items():
        seen.add(name)
        p_row = proposal_rows.get(name)
        latest = _read_latest_metric(svc, name, f.factor_version)
        decay_verdict = _compute_decay_verdict(svc, name)
        rows.append({
            "name": name,
            "factor_version": f.factor_version,
            "direction": f.direction,
            "lookback_days": f.lookback_days,
            "status": _classify(name, True, p_row),
            "last_metric": latest,
            "decay": decay_verdict,
            "composite_weight": composite_weights.get(name),
            "selected_top50": name in selected_factor_names,
            "oos_observations": p_row["oos_observations"] if p_row else None,
            "oos_ir": p_row["oos_ir"] if p_row else None,
        })

    # 5.b) 再遍历 proposal_store 里 registry 没有的 factor (shadow / 历史 rejected / demoted)
    for name, p_row in proposal_rows.items():
        if name in seen:
            continue
        # 这类因子不在 registry, 没有 Factor 实例, lookback_days/direction 用 proposal 信息兜底
        latest = _read_latest_metric(svc, name, factor_version=1)
        rows.append({
            "name": name,
            "factor_version": 1,
            "direction": None,  # shadow/rejected 在 proposals 表里有 direction 列, 这里偷懒不查, UI 不强需
            "lookback_days": None,
            "status": _classify(name, False, p_row),
            "last_metric": latest,
            "decay": None,
            "composite_weight": None,  # 不参与组合
            "selected_top50": False,
            "oos_observations": p_row["oos_observations"],
            "oos_ir": p_row["oos_ir"],
            # shadow/rejected 因子的 IS-IC 直接给出 (auto_* 在 factor_proposals.ic_mean/ir 里有值)
            "is_ic_mean": p_row["p_ic_mean"],
            "is_ir": p_row["p_ir"],
        })

    return {
        "factors": rows,
        "n": len(rows),
        "snapshot_date": latest_snapshot_date,
    }


def _read_latest_metric(svc: ServiceContainer, name: str, factor_version: int) -> dict[str, Any] | None:
    """读 factor_metrics 最近一行; 找不到返回 None."""
    if svc.factor_evaluator is None:
        return None
    try:
        m = svc.factor_evaluator.get_latest(name, factor_version)
    except Exception:
        m = None
    # M19 兼容: shadow/rejected 因子可能 factor_version=1 取不到, fallback 用 list_history(limit=1)
    if m is None:
        try:
            history = svc.factor_evaluator.list_history(name, limit=1)
            if history:
                m = history[0]
        except Exception:
            m = None
    if m is None:
        return None
    return {
        "as_of_date": m.as_of_date,
        "window_days": m.window_days,
        "ic_mean": m.ic_mean,
        "ir": m.ir,
        "status": m.status,
    }


def _compute_decay_verdict(svc: ServiceContainer, name: str) -> dict[str, Any] | None:
    """P1-4: 30 天 IR 历史算衰减判定; 没数据返回 None."""
    if svc.factor_evaluator is None:
        return None
    try:
        history = svc.factor_evaluator.list_history(name, limit=30)
        irs = [float(m.ir) for m in history if m.ir is not None]
        if len(irs) < 6:
            return None
        mid = len(irs) // 2
        ir_recent = sum(abs(x) for x in irs[:mid]) / mid
        ir_earlier = sum(abs(x) for x in irs[mid:]) / max(len(irs) - mid, 1)
        ir_peak = max(abs(x) for x in irs)
        ir_now = abs(irs[0])
        if ir_earlier > 0.1 and ir_recent < 0.6 * ir_earlier:
            return {"level": "severe", "label": "⚠️ 显著衰减",
                    "ir_recent": ir_recent, "ir_earlier": ir_earlier}
        if ir_earlier > 0.1 and ir_recent < 0.8 * ir_earlier:
            return {"level": "mild", "label": "轻微衰减",
                    "ir_recent": ir_recent, "ir_earlier": ir_earlier}
        if ir_now < 0.6 * ir_peak and ir_peak > 0.2:
            return {"level": "off_peak", "label": "已离峰值",
                    "ir_now": ir_now, "ir_peak": ir_peak}
    except Exception:
        pass
    return None


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

    # accept 时同步算一次 IS-IC, 与 auto_* 候选对齐 (auto_* 在 DiscoveryEngine 里
    # 通过 evaluator.evaluate 把 ic_mean/ir 写进 factor_proposals; LLM 因子之前漏了这一步,
    # 导致 shadow 期前 20 天用户在 UI 上看到的 LLM 因子 IR/IC 全是 NULL, 没法做接受决策).
    # 失败不影响 accept 本身, 只是 IS-IC 暂时缺失, 第二天 batch.deep_research 会补上。
    is_ic_result: dict[str, Any] | None = None
    if action == "accept":
        is_ic_result = _evaluate_is_ic_for_llm_factor(svc, factor_name)

    return {
        "ok": True,
        "factor_name": factor_name,
        "status": new_status,
        "is_ic": is_ic_result,
    }


def _evaluate_is_ic_for_llm_factor(svc: ServiceContainer, factor_name: str) -> dict[str, Any] | None:
    """LLM accept 后算一次 IS IC/IR 回填到 factor_proposals。

    复用 DiscoveryEngine 已经实现的 _prepare_data + _compute_factor_history 逻辑,
    避免重写。失败返回 None, 上游不抛。
    """
    import logging as _logging
    from datetime import date as _d
    from datetime import timedelta as _td

    from akq_agents.services.data.repository import open_meta_db as _open_db
    from akq_agents.services.factors.discovery import make_factor
    from akq_agents.services.factors.proposal_store import (
        FactorProposal,
        now_iso as _now_iso,
        recipe_from_json,
    )

    log = _logging.getLogger(__name__)
    try:
        engine = (svc.workflow.services.get("discovery_engine") if svc.workflow else None)
        evaluator = (svc.workflow.services.get("factor_evaluator") if svc.workflow else None)
        store = _proposal_store(svc)
        if engine is None or evaluator is None or store is None:
            return {"ok": False, "reason": "services_not_available"}

        if svc.repo is None:
            return {"ok": False, "reason": "repo_unavailable"}
        db_path = svc.repo._base_dir / "meta.db"
        with _open_db(db_path) as conn:
            row = conn.execute(
                "SELECT recipe_json, direction, max_abs_corr, created_at, shadow_started_at "
                "FROM factor_proposals WHERE factor_name=?",
                (factor_name,),
            ).fetchone()
        if row is None:
            return {"ok": False, "reason": "proposal_not_found"}
        recipe_json, direction, max_abs_corr, created_at, shadow_started_at = row
        recipe = recipe_from_json(recipe_json)
        factor = make_factor(recipe)
        # 保留 LLM 命名 (含 hash 后缀), 不让 make_factor 算出来的 name 覆盖
        try:
            factor.name = factor_name  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        # 准备数据 (复用 DiscoveryEngine._prepare_data); empty 时 fallback 用昨日
        as_of = _d.today()
        ohlcv, _syms = engine._prepare_data(as_of)  # type: ignore[attr-defined]
        if ohlcv.empty:
            ohlcv, _syms = engine._prepare_data(as_of - _td(days=1))  # type: ignore[attr-defined]
        if ohlcv.empty:
            return {"ok": False, "reason": "no_data"}

        close = ohlcv.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        forward_returns = close.pct_change(fill_method=None).shift(-1)

        # 算 factor_history (复用 DiscoveryEngine._compute_factor_history)
        factor_history = engine._compute_factor_history(factor, ohlcv, close.index)  # type: ignore[attr-defined]
        if factor_history is None or factor_history.empty:
            return {"ok": False, "reason": "factor_history_empty"}
        common_idx = factor_history.index.intersection(forward_returns.index)
        if len(common_idx) < 20:
            return {"ok": False, "reason": f"insufficient_history_{len(common_idx)}"}

        # evaluate -> 写 factor_metrics 表 (factor.name 已改成 llm_xxx, 写入 row 落 llm_xxx)
        metric = evaluator.evaluate(
            factor=factor,
            factor_history=factor_history.loc[common_idx],
            forward_returns=forward_returns.loc[common_idx],
            as_of_date=as_of,
        )

        # 把 IS-IC 回填到 factor_proposals (与 auto_* 对齐)
        proposal = FactorProposal(
            factor_name=factor_name,
            recipe_json=recipe_json,
            direction=direction,
            status="shadow",
            ic_mean=metric.ic_mean,
            ic_std=metric.ic_std,
            ir=metric.ir,
            t_stat=metric.t_stat,
            max_abs_corr=max_abs_corr,
            reason="accepted_from_llm_passed_is",
            created_at=created_at,
            evaluated_at=_now_iso(),
            shadow_started_at=shadow_started_at,
            oos_observations=0,
            oos_ir=None,
        )
        store.upsert(proposal)

        return {
            "ok": True,
            "ic_mean": metric.ic_mean,
            "ir": metric.ir,
            "t_stat": metric.t_stat,
            "n_obs": len(common_idx),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("evaluate_is_ic for %s failed: %s", factor_name, exc)
        return {"ok": False, "reason": f"exception: {exc}"}


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
        raise HTTPException(404, f"该日期无组合快照: {date}")

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

    # 兜底 contributor/dragger 的中文简称（snapshot 表 name 列历史为空）
    top_contrib = contribs[:5]
    top_drag = list(reversed(contribs[-5:]))
    _backfill_names(svc, top_contrib)
    _backfill_names(svc, top_drag)

    return {
        "date": date,
        "n_holdings": len(rows),
        "n_with_return": len(contribs),
        "top_contributors": top_contrib,
        "top_draggers": top_drag,
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

    # 如果 user 不传 date，默认用最近一个 ohlcv parquet partition（不是 today，
    # 因为当日数据要等 16:00 cron 才会刷出来；今天没数据时 endpoint 不该 404）。
    if date:
        target_d = _date.fromisoformat(date)
    else:
        target_d = _latest_ohlcv_partition(svc.repo) or _date.today()
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
        raise HTTPException(404, f"无可用 OHLCV 数据 ({target_d.isoformat()})；可能尚未拉取或解析失败")

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


def _latest_ohlcv_partition(repo) -> _date | None:
    """扫 ohlcv parquet 目录找最大日期分区。今日 cron 没跑完时用作 fallback。"""
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    if ohlcv_dir is None or not ohlcv_dir.exists():
        return None
    candidates = []
    for p in ohlcv_dir.iterdir():
        # 分区目录名形如 date=2026-06-23
        name = p.name
        if name.startswith("date=") and len(name) == 15:
            try:
                candidates.append(_date.fromisoformat(name[5:]))
            except ValueError:
                continue
    return max(candidates) if candidates else None
