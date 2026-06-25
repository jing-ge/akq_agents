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


def test_mark_executed_buy_writes_avg_cost_for_new_position(tmp_path: Path) -> None:
    """B2: BUY 建仓时，avg_cost 应写成 current_price。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000010", target=1000.0, price=15.0)])
    tl.mark_executed(cohort_d, "000010", holdings_store=h)

    assert h.get_shares("000010") == 1000.0
    assert h.get_avg_cost("000010") == 15.0


def test_mark_executed_buy_uses_real_holdings_not_cohort_snapshot(tmp_path: Path) -> None:
    """B2 边界 1: 用户在 cohort 生成后手工加仓，mark_executed 不应覆盖真实成本。

    场景:
      - cohort 写入时 current_shares=0, target=1000, price=20.0
      - 用户在 mark 之前 PUT /holdings 加了 500 股，成本 18.0
      - mark_executed 应识别 real_current=500, 只补买 500 股, 加权平均 = (500*18 + 500*20)/1000 = 19.0
    """
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000020", target=1000.0, price=20.0)])

    # 用户中途手工加仓
    h.upsert("000020", shares=500.0, avg_cost=18.0)

    tl.mark_executed(cohort_d, "000020", holdings_store=h)

    assert h.get_shares("000020") == 1000.0
    avg = h.get_avg_cost("000020")
    assert avg is not None
    assert abs(avg - 19.0) < 1e-6


def test_mark_executed_skips_holdings_if_user_already_filled(tmp_path: Path) -> None:
    """B2 边界 1: 用户已自购到目标（real >= target），mark 仅标记 executed，不动 holdings。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000030", target=1000.0, price=20.0)])
    # 用户已经自购到 1200 股，成本 17.0
    h.upsert("000030", shares=1200.0, avg_cost=17.0)

    tl.mark_executed(cohort_d, "000030", holdings_store=h)

    # holdings 保持不变（不被 cohort target 覆盖）
    assert h.get_shares("000030") == 1200.0
    assert h.get_avg_cost("000030") == 17.0
    # 仍然标记 executed
    items = tl.list_cohort(cohort_d)
    assert items[0]["executed"] is True


def test_mark_executed_sell_preserves_avg_cost(tmp_path: Path) -> None:
    """B2: SELL 减仓不应改 avg_cost（卖出不影响剩余股的成本基准）。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    h.upsert("000040", shares=1000.0, avg_cost=12.0)

    cohort_d = date(2026, 6, 23)
    sell_item = TradeItem(
        symbol="000040",
        action="SELL",
        current_shares=1000.0,
        target_shares=400.0,
        delta_shares=-600.0,
        target_weight=0.02,
        current_price=15.0,
        delta_amount=-9000.0,
        reason="SELL partial",
        industry="银行",
        composite_score=0.5,
    )
    tl.upsert_cohort(cohort_d, [sell_item])
    tl.mark_executed(cohort_d, "000040", holdings_store=h)

    assert h.get_shares("000040") == 400.0
    assert h.get_avg_cost("000040") == 12.0  # 不变


def test_upsert_cohort_preserves_executed_rows(tmp_path: Path) -> None:
    """P1-1: recompute 写入 cohort 时不应删掉已 executed=1 的历史。"""
    db = tmp_path / "meta.db"
    tl = TradeListStore(db)
    h = HoldingsStore(db)

    cohort_d = date(2026, 6, 23)
    tl.upsert_cohort(cohort_d, [_make_buy_item("000050", target=500.0)])
    tl.mark_executed(cohort_d, "000050", holdings_store=h)

    # recompute: 新清单不包含 000050（因为用户已执行 → holdings 满足目标 → 不再出现）
    tl.upsert_cohort(cohort_d, [_make_buy_item("000051", target=300.0)])

    items = {it["symbol"]: it for it in tl.list_cohort(cohort_d)}
    # 已执行的历史应保留
    assert "000050" in items
    assert items["000050"]["executed"] is True
    # 新清单应包含
    assert "000051" in items
