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


@router.get("/proposals/{factor_name}/trace")
async def proposal_trace(factor_name: str) -> dict[str, Any]:
    """某个因子的完整推理详情：recipe + 评估指标 + 历史 metrics + 决策路径。"""
    svc: ServiceContainer = get_services()
    if svc.proposal_store is None:
        return {"error": "no_proposal_store"}

    # 读 proposal 主记录
    rows = svc.proposal_store.list_recent(limit=500)
    target = next((r for r in rows if r.factor_name == factor_name), None)
    if target is None:
        return {"error": "not_found", "factor_name": factor_name}

    recipe = json.loads(target.recipe_json)
    # 用易懂语言重写 recipe
    op_cn = {
        "pct_change": "百分比变化", "rolling_mean": "滚动均值",
        "rolling_std": "滚动标准差", "zscore": "Z 分数",
        "rsi": "RSI 指标", "rolling_skew": "滚动偏度",
        "ts_max_norm": "归一化最大值", "ts_min_norm": "归一化最小值",
    }
    base_cn = {"close": "收盘价", "volume": "成交量", "amount": "成交额",
               "high_low_range": "最高-最低价差", "vwap": "成交均价"}
    dir_cn = {"long": "做多（值大持有）", "short": "做空（值小持有）"}
    plain = f"对 {base_cn.get(recipe['base'], recipe['base'])} 做 {op_cn.get(recipe['op'], recipe['op'])}，窗口 {recipe['window']} 天，方向 {dir_cn.get(recipe['direction'], recipe['direction'])}"

    # 决策路径解读
    decisions = []
    if target.ic_mean is not None:
        decisions.append({
            "step": "IS（样本内）评估",
            "result": f"IC={target.ic_mean:.4f}, IR={target.ir:.3f}" if target.ir else f"IC={target.ic_mean:.4f}",
            "pass": abs(target.ic_mean) >= 0.015 and (target.ir is None or abs(target.ir) >= 0.30),
            "threshold": "|IC|≥0.015 且 |IR|≥0.30",
        })
    if target.max_abs_corr is not None:
        decisions.append({
            "step": "相关性筛查",
            "result": f"与已有因子最大相关性 = {target.max_abs_corr:.3f}",
            "pass": target.max_abs_corr <= 0.7,
            "threshold": "max|corr| ≤ 0.7",
        })
    if target.shadow_started_at:
        decisions.append({
            "step": "进入 Shadow（OOS 观察）",
            "result": f"开始时间: {target.shadow_started_at}",
            "pass": True,
        })
    if target.oos_observations is not None and target.oos_observations > 0:
        passed = target.oos_observations >= 20 and target.oos_ir is not None and abs(target.oos_ir) >= 0.15
        decisions.append({
            "step": "OOS（样本外）验证",
            "result": f"观察 {target.oos_observations} 个交易日，OOS IR = {target.oos_ir:.3f}" if target.oos_ir else f"观察 {target.oos_observations} 天，OOS IR 未达标",
            "pass": passed,
            "threshold": "≥20 天观察 且 |OOS IR| ≥ 0.15",
        })

    # 历史 metrics（如果是已注册因子，会有 factor_metrics 历史）
    metrics_history = []
    if svc.factor_evaluator is not None:
        try:
            metrics_history = [
                {
                    "as_of_date": m.as_of_date,
                    "ic_mean": m.ic_mean,
                    "ir": m.ir,
                    "t_stat": m.t_stat,
                    "status": m.status,
                    "reason": m.reason,
                }
                for m in svc.factor_evaluator.list_history(factor_name, limit=60)
            ]
        except Exception:
            metrics_history = []

    return {
        "factor_name": factor_name,
        "status": target.status,
        "recipe": recipe,
        "plain_description": plain,
        "decisions": decisions,
        "metrics": {
            "ic_mean": target.ic_mean,
            "ic_std": target.ic_std,
            "ir": target.ir,
            "t_stat": target.t_stat,
            "max_abs_corr": target.max_abs_corr,
            "oos_observations": target.oos_observations,
            "oos_ir": target.oos_ir,
        },
        "reason": target.reason,
        "created_at": target.created_at,
        "evaluated_at": target.evaluated_at,
        "shadow_started_at": target.shadow_started_at,
        "metrics_history": metrics_history,
        "n_history": len(metrics_history),
    }


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


# ============================================================
# P0-2 Paper Trading 前向跟踪
# ============================================================


@router.get("/paper-trading/summary")
async def paper_trading_summary() -> dict[str, Any]:
    """所有 cohort 在 30/60/90 天后的平均表现。"""
    svc: ServiceContainer = get_services()
    if svc.paper_trading_store is None:
        return {"error": "no_paper_trading"}
    return svc.paper_trading_store.summary()


@router.get("/paper-trading/cohorts")
async def paper_trading_cohorts(limit: int = 60) -> dict[str, Any]:
    """所有 cohort 列表 + 各自最新表现。"""
    svc: ServiceContainer = get_services()
    if svc.paper_trading_store is None:
        return {"cohorts": [], "n": 0}
    rows = svc.paper_trading_store.list_cohorts(limit=limit)
    return {"cohorts": rows, "n": len(rows)}


@router.get("/paper-trading/cohorts/{cohort_date}/timeseries")
async def paper_trading_cohort_timeseries(cohort_date: str) -> dict[str, Any]:
    """某 cohort 的逐日表现时序。"""
    svc: ServiceContainer = get_services()
    if svc.paper_trading_store is None:
        return {"timeseries": [], "n": 0}
    rows = svc.paper_trading_store.get_cohort_timeseries(cohort_date)
    return {"cohort_date": cohort_date, "timeseries": rows, "n": len(rows)}
