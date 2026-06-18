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
    """某个因子的完整推理详情：recipe + 评估指标 + 历史 metrics + 决策路径。

    支持 auto 因子（来自 factor_proposals）和原生因子（来自 factor_registry）。
    """
    svc: ServiceContainer = get_services()

    # 1) 尝试找 proposal（auto 因子）
    target = None
    if svc.proposal_store is not None:
        rows = svc.proposal_store.list_recent(limit=500)
        target = next((r for r in rows if r.factor_name == factor_name), None)

    # 2) 没找到 proposal 但 registry 里有 → 原生因子，从 registry + factor_metrics 拼 trace
    if target is None:
        if svc.factor_registry is not None:
            try:
                f = svc.factor_registry.get(factor_name)
            except Exception:
                f = None
            if f is not None:
                # 解析原生因子名（momentum_5 / log_amount_20 / volatility_20 等）
                return _build_native_factor_trace(svc, f)
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


# ============================================================
# L-Q5: 原生因子推理（不在 proposal_store 时的兜底）
# ============================================================


def _build_native_factor_trace(svc, factor) -> dict[str, Any]:
    """原生因子（momentum_5 等）走的兜底路径。

    从 factor_registry + factor_metrics 拼一份和 proposal trace 结构兼容的字典。
    """
    # 用 factor.name 反推 base/op/window/direction 给 plain_description
    name = factor.name
    direction = factor.direction
    plain = _describe_native_factor(name, direction)

    # 历史 metrics
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
                for m in svc.factor_evaluator.list_history(name, limit=60)
            ]
        except Exception:
            metrics_history = []

    # 最新指标
    latest = None
    if metrics_history:
        latest = metrics_history[0]

    # 决策路径：原生因子只有"评估" + "失能判定"
    decisions = []
    if latest and latest.get("ir") is not None:
        ir = float(latest["ir"])
        ic = float(latest["ic_mean"]) if latest.get("ic_mean") is not None else 0.0
        as_of = latest["as_of_date"]
        reason_str = latest.get("reason") or "-"
        decisions.append({
            "step": "原生因子（启动期手工注册）",
            "result": "无需 OOS 验证，直接进入组合",
            "pass": True,
        })
        decisions.append({
            "step": f"最近 IC/IR 评估（{as_of}）",
            "result": f"IC={ic:.4f}, IR={ir:.3f}",
            "pass": abs(ir) >= 0.15,
            "threshold": "|IR| ≥ 0.15 才保持 active",
        })
        if latest.get("status") == "inactive":
            decisions.append({
                "step": "自动失能判定",
                "result": f"status=inactive（原因：{reason_str}）",
                "pass": False,
                "threshold": "连续低 IR 自动 disable",
            })

    return {
        "factor_name": name,
        "status": "native",
        "recipe": {
            "type": "native",
            "name": name,
            "direction": direction,
            "lookback_days": getattr(factor, "lookback_days", None),
        },
        "plain_description": plain,
        "decisions": decisions,
        "metrics": {
            "ic_mean": latest.get("ic_mean") if latest else None,
            "ir": latest.get("ir") if latest else None,
            "t_stat": latest.get("t_stat") if latest else None,
        },
        "reason": "native_factor",
        "created_at": None,
        "evaluated_at": latest.get("as_of_date") if latest else None,
        "shadow_started_at": None,
        "metrics_history": metrics_history,
        "n_history": len(metrics_history),
    }


def _describe_native_factor(name: str, direction: str) -> str:
    """把 momentum_5 / reversal_5 / volatility_20 / amount_20 等翻译成人话。"""
    dir_cn = {"long": "做多（值大持有）", "short": "做空（值小持有）"}
    map_ = {
        "momentum_5":      "5 日动量：close 比 5 天前涨多少",
        "momentum_20":     "20 日动量：close 比 20 天前涨多少",
        "momentum_60":     "60 日动量：close 比 60 天前涨多少",
        "reversal_5":      "5 日反转：5 日跌得越多分越高（动量取负）",
        "volatility_20":   "20 日波动率：过去 20 日日收益率标准差",
        "amount_20":       "20 日日均成交额（流动性代理）",
        "log_amount_20":   "20 日日均成交额取对数（规模代理）",
    }
    base = map_.get(name, f"原生因子 {name}")
    return f"{base}，方向 {dir_cn.get(direction, direction)}"

