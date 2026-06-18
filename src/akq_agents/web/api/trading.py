"""Trading endpoints（P0-1）：实盘持仓 + 今日交易清单。"""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException

from akq_agents.web.deps import get_services

router = APIRouter()


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
    """手动校准持仓：shares / avg_cost / note。"""
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
    return {"status": "ok", "symbol": symbol, "shares": shares}


@router.delete("/holdings/{symbol}")
async def delete_holding(symbol: str) -> dict[str, Any]:
    """删除一只持仓（等价于 shares=0）。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        raise HTTPException(503, "holdings_store not ready")
    store.upsert(symbol, 0.0)
    return {"status": "ok"}


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

    return {
        "cohort_date": target_date.isoformat(),
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
