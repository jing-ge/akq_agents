"""Trading endpoints（P0-1）：实盘持仓 + 今日交易清单。"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException

from akq_agents.web.deps import get_services

logger = logging.getLogger(__name__)
router = APIRouter()


def _recompute_today_trade_list() -> dict[str, Any]:
    """holdings 改后立即重算今日 trade_list（基于最新 portfolio_snapshot）。

    返回：{recomputed: bool, n_items: int, error: str | None}
    """
    svc = get_services()
    workflow = svc.workflow
    if workflow is None:
        return {"recomputed": False, "error": "workflow not ready"}
    services = workflow.services
    snap_store = services.get("portfolio_snapshot_store")
    holdings_store = services.get("holdings_store")
    tl_store = services.get("trade_list_store")
    tl_cfg = services.get("trade_list_config")
    repo = services.get("data_repository")
    ind_store = services.get("industry_map_store")

    if not all([snap_store, holdings_store, tl_store, tl_cfg, repo]):
        return {"recomputed": False, "error": "missing services"}

    # 找最新有 snapshot 的日期
    snapshot_dates = snap_store.list_dates(limit=1)
    if not snapshot_dates:
        return {"recomputed": False, "error": "no snapshots"}
    target_date = _date.fromisoformat(snapshot_dates[0])

    # 读 snapshot → 权重 + 上一日权重
    rows = snap_store.read_snapshot(target_date)
    if not rows:
        return {"recomputed": False, "error": "no snapshot for today"}
    weights = {r.symbol: float(r.weight) for r in rows}
    composite = {r.symbol: float(r.composite_score or 0.0) for r in rows}
    # prev: 用 snapshot 表里上一日
    prev_weights_series = snap_store.read_prev_weights(target_date)
    prev_weights = {str(s): float(w) for s, w in prev_weights_series.items()} if not prev_weights_series.empty else {}

    # 拿当日 close（从 ohlcv parquet 查 weights 里的 symbol + holdings 里的 symbol）
    holdings_dict = holdings_store.as_dict()
    all_syms = set(weights.keys()) | set(holdings_dict.keys())
    import pyarrow.dataset as ds
    from datetime import timedelta
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    today_close: dict[str, float] = {}
    if ohlcv_dir and ohlcv_dir.exists() and all_syms:
        try:
            start = (target_date - timedelta(days=7)).isoformat()
            end = target_date.isoformat()
            dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
            table = dataset.to_table(
                filter=(ds.field("date") >= start)
                       & (ds.field("date") <= end)
                       & ds.field("symbol").isin(list(all_syms)),
                columns=["date", "symbol", "close"],
            )
            df = table.to_pandas()
            if not df.empty:
                df = df.sort_values(["symbol", "date"])
                latest = df.groupby("symbol").tail(1)
                for _, r in latest.iterrows():
                    today_close[str(r["symbol"])] = float(r["close"])
        except Exception as exc:
            logger.warning("close lookup in recompute failed: %s", exc)

    # 行业映射
    industry_name_map = ind_store.load_names() if ind_store else {}

    from akq_agents.services.portfolio.trade_list import generate_trade_list

    items = generate_trade_list(
        cohort_date=target_date,
        target_weights=weights,
        current_close=today_close,
        holdings=holdings_dict,
        composite_scores=composite,
        industry_map=industry_name_map,
        yesterday_weights=prev_weights,
        cfg=tl_cfg,
    )
    tl_store.upsert_cohort(target_date, items)
    return {"recomputed": True, "cohort_date": target_date.isoformat(), "n_items": len(items)}


@router.get("/holdings")
async def list_holdings() -> dict[str, Any]:
    """列出当前真实持仓。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        return {"holdings": [], "n": 0}
    rows = store.list_all()
    total_shares = sum(float(r["shares"]) for r in rows)
    return {"holdings": rows, "n": len(rows), "total_shares": total_shares}


@router.put("/holdings/{symbol}")
async def upsert_holding(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """手动校准持仓：shares / avg_cost / note。修改后立即重算今日 trade_list。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        raise HTTPException(503, "holdings_store not ready")
    shares = float(payload.get("shares", 0))
    avg_cost = payload.get("avg_cost")
    if avg_cost is not None:
        avg_cost = float(avg_cost)
    note = payload.get("note")
    store.upsert(symbol, shares, avg_cost=avg_cost, note=note)

    # L-1: 持仓改了 → 重算今日 trade_list
    recompute_result = _recompute_today_trade_list()

    return {"status": "ok", "symbol": symbol, "shares": shares, "recompute": recompute_result}


@router.delete("/holdings/{symbol}")
async def delete_holding(symbol: str) -> dict[str, Any]:
    """删除一只持仓（等价于 shares=0）。修改后立即重算今日 trade_list。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        raise HTTPException(503, "holdings_store not ready")
    store.upsert(symbol, 0.0)
    recompute_result = _recompute_today_trade_list()
    return {"status": "ok", "recompute": recompute_result}


@router.post("/holdings/recompute")
async def recompute_trade_list_manual() -> dict[str, Any]:
    """手动触发 trade_list 重算（用于调试 / 用户主动刷新）。"""
    result = _recompute_today_trade_list()
    if not result.get("recomputed"):
        raise HTTPException(503, result.get("error", "unknown"))
    return result


@router.get("/today-list")
async def today_trade_list(date: str | None = None) -> dict[str, Any]:
    """获取某日（默认今日）的交易清单。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("trade_list_store") if workflow else None
    if store is None:
        return {"items": [], "n": 0}
    target_date = _date.fromisoformat(date) if date else _date.today()
    items = store.list_cohort(target_date)

    if not items:
        # 找最近一天有清单的日期作为回退
        dates = store.list_dates(limit=1)
        if dates:
            target_date = _date.fromisoformat(dates[0])
            items = store.list_cohort(target_date)

    # 汇总
    n_buy = sum(1 for it in items if it["action"] == "BUY")
    n_sell = sum(1 for it in items if it["action"] == "SELL")
    n_hold = sum(1 for it in items if it["action"] == "HOLD")
    total_buy_amt = sum(it["delta_amount"] for it in items if it["action"] == "BUY")
    total_sell_amt = sum(abs(it["delta_amount"]) for it in items if it["action"] == "SELL")

    # 修复 oracle #3：标注 staleness 让前端 / LLM 都能感知
    today_actual = _date.today()
    staleness_days = (today_actual - target_date).days
    is_today = staleness_days == 0

    return {
        "cohort_date": target_date.isoformat(),
        "today": today_actual.isoformat(),
        "is_today": is_today,
        "staleness_days": staleness_days,
        "stale_warning": None if is_today else (
            f"⚠️ 当前清单生成于 {staleness_days} 天前（{target_date.isoformat()}），"
            f"今日盘后 15:30 会自动刷新；当前清单仅供参考，不代表今日盘中实时建议。"
        ),
        "n": len(items),
        "n_buy": n_buy,
        "n_sell": n_sell,
        "n_hold": n_hold,
        "total_buy_amount": total_buy_amt,
        "total_sell_amount": total_sell_amt,
        "items": items,
    }


@router.post("/today-list/{symbol}/mark-executed")
async def mark_executed(symbol: str, date: str | None = None) -> dict[str, Any]:
    """标记某条交易已执行（点击 ✓ 时调用）。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("trade_list_store") if workflow else None
    if store is None:
        raise HTTPException(503, "trade_list_store not ready")
    target_date = _date.fromisoformat(date) if date else _date.today()
    store.mark_executed(target_date, symbol)
    return {"status": "ok"}


@router.get("/dates")
async def list_trade_list_dates() -> dict[str, Any]:
    """所有有清单的日期（最近 30 天）。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("trade_list_store") if workflow else None
    if store is None:
        return {"dates": []}
    return {"dates": store.list_dates(limit=30)}
