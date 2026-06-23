"""D1 闭环 regression: mark_executed 应同时更新 holdings。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from akq_agents.services.portfolio.trade_list import HoldingsStore, TradeItem, TradeListStore


def _make_buy_item(symbol: str, target: float, price: float = 10.0) -> TradeItem:
    return TradeItem(
        symbol=symbol,
        action="BUY",
        current_shares=0.0,
        target_shares=target,
        delta_shares=target,
        target_weight=0.05,
        current_price=price,
        delta_amount=target * price,
        reason="BUY new",
        industry="银行",
        composite_score=1.0,
    )


def test_mark_executed_writes_to_holdings(tmp_path: Path) -> None:
    """D1: mark_executed 应同时把 target_shares 写到 holdings。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000001", target=1000.0)])

    assert h.get_shares("000001") == 0.0

    tl.mark_executed(cohort_d, "000001", holdings_store=h)

    assert h.get_shares("000001") == 1000.0


def test_mark_executed_sell_to_zero_deletes_holding(tmp_path: Path) -> None:
    """D1: target_shares=0 时 (SELL all) 应删 holdings 行。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    h.upsert("000002", shares=500.0)
    assert h.get_shares("000002") == 500.0

    cohort_d = date(2026, 6, 23)
    sell_item = TradeItem(
        symbol="000002",
        action="SELL",
        current_shares=500.0,
        target_shares=0.0,
        delta_shares=-500.0,
        target_weight=0.0,
        current_price=12.0,
        delta_amount=-6000.0,
        reason="SELL all",
        industry="银行",
        composite_score=0.5,
    )
    tl.upsert_cohort(cohort_d, [sell_item])
    tl.mark_executed(cohort_d, "000002", holdings_store=h)

    assert h.get_shares("000002") == 0.0


def test_mark_executed_without_holdings_store_keeps_old_behavior(tmp_path: Path) -> None:
    """D1: 不传 holdings_store 时维持原行为（只标 executed=1，不写 holdings）。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)
    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000003", target=200.0)])

    tl.mark_executed(cohort_d, "000003")

    assert h.get_shares("000003") == 0.0
    items = tl.list_cohort(cohort_d)
    assert items[0]["executed"] is True
