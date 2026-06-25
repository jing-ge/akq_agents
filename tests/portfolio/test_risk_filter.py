"""RiskFilter.apply 行为单测，重点验证 same-day 停牌检测。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from akq_agents.services.portfolio.risk_filter import RiskFilter, RiskFilterConfig


def _ohlcv_rows(symbol: str, days: list[str], close: float, volume: float, amount: float) -> list[dict]:
    return [
        {"date": d, "symbol": symbol, "close": close, "volume": volume, "amount": amount}
        for d in days
    ]


def test_risk_filter_keeps_normal_stock() -> None:
    cfg = RiskFilterConfig(
        min_listing_days=5, min_price=1.0, max_price=1000.0,
        min_avg_amount=100_000.0, amount_window=5,
    )
    rf = RiskFilter(cfg)
    dates = [f"2026-06-{d:02d}" for d in range(15, 26)]
    ohlcv = pd.DataFrame(_ohlcv_rows("600000", dates, 10.0, 1e6, 1e7))
    res = rf.apply(["600000"], ohlcv, as_of_date=date(2026, 6, 25))
    assert "600000" in res.kept
    assert "600000" not in res.excluded


def test_risk_filter_detects_same_day_suspension() -> None:
    """R4 关键回归：as_of_date 当天数据缺失 → 视为停牌，而非用更早一日的成交记录蒙混。

    场景：6/15 ~ 6/24 都正常成交（volume>0），6/25 当天停牌（无任何行）。
    之前 sub.iloc[-1] 会取 6/24 的行 → 判断为未停牌 → 漏检。
    """
    cfg = RiskFilterConfig(
        min_listing_days=5, min_price=1.0, max_price=1000.0,
        min_avg_amount=100_000.0, amount_window=5,
    )
    rf = RiskFilter(cfg)
    dates = [f"2026-06-{d:02d}" for d in range(15, 25)]  # 缺 6/25
    ohlcv = pd.DataFrame(_ohlcv_rows("600001", dates, 10.0, 1e6, 1e7))
    res = rf.apply(["600001"], ohlcv, as_of_date=date(2026, 6, 25))
    assert "600001" in res.excluded
    assert res.excluded["600001"] == "SUSPENDED"


def test_risk_filter_detects_zero_volume_on_target_day() -> None:
    """同日 volume=0 也是停牌。"""
    cfg = RiskFilterConfig(
        min_listing_days=5, min_price=1.0, max_price=1000.0,
        min_avg_amount=100_000.0, amount_window=5,
    )
    rf = RiskFilter(cfg)
    dates = [f"2026-06-{d:02d}" for d in range(15, 25)]
    rows = _ohlcv_rows("600002", dates, 10.0, 1e6, 1e7)
    # 6/25 当天停牌 (volume=0)
    rows.append({"date": "2026-06-25", "symbol": "600002", "close": 10.0, "volume": 0.0, "amount": 0.0})
    ohlcv = pd.DataFrame(rows)
    res = rf.apply(["600002"], ohlcv, as_of_date=date(2026, 6, 25))
    assert res.excluded.get("600002") == "SUSPENDED"


def test_risk_filter_excludes_low_liquidity() -> None:
    """低流动性应被剔除，且 same-day 检测不应误伤正常股。"""
    cfg = RiskFilterConfig(
        min_listing_days=5, min_price=1.0, max_price=1000.0,
        min_avg_amount=10_000_000.0, amount_window=5,  # 阈值高
    )
    rf = RiskFilter(cfg)
    dates = [f"2026-06-{d:02d}" for d in range(15, 26)]
    ohlcv = pd.DataFrame(_ohlcv_rows("600003", dates, 10.0, 1e4, 1e5))  # amount 远低于阈值
    res = rf.apply(["600003"], ohlcv, as_of_date=date(2026, 6, 25))
    assert "600003" in res.excluded
    assert res.excluded["600003"].startswith("LOW_LIQUIDITY")
