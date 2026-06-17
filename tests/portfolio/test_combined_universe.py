"""CombinedUniverseBuilder 单元测试。"""

from __future__ import annotations

import pandas as pd

from akq_agents.services.portfolio.combined_universe import build_portfolio_universe


def _make_amounts(symbol_amounts: dict[str, list[float]]) -> pd.DataFrame:
    """构造测试 OHLCV：每只股票按 symbol_amounts[sym] 指定的 amount 序列。"""
    rows = []
    for sym, amounts in symbol_amounts.items():
        for i, amt in enumerate(amounts):
            rows.append(
                {
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                    "symbol": sym,
                    "open": 0,
                    "high": 0,
                    "low": 0,
                    "close": 10.0,
                    "volume": 1.0,
                    "amount": amt,
                }
            )
    return pd.DataFrame(rows)


def test_top_n_by_amount_mean() -> None:
    ohlcv = _make_amounts({
        "HIGH": [100.0] * 20,
        "MID":  [50.0] * 20,
        "LOW":  [10.0] * 20,
    })
    out = build_portfolio_universe(
        full_universe_symbols=["HIGH", "MID", "LOW"],
        ohlcv=ohlcv,
        top_n=2,
        window=20,
    )
    assert out == ["HIGH", "MID"]


def test_top_n_larger_than_universe_returns_all_sorted() -> None:
    ohlcv = _make_amounts({"A": [1.0] * 20, "B": [2.0] * 20})
    out = build_portfolio_universe(
        full_universe_symbols=["A", "B"], ohlcv=ohlcv, top_n=100, window=20
    )
    assert set(out) == {"A", "B"}
    # 按 amount 降序
    assert out == ["B", "A"]


def test_missing_amount_falls_to_zero_and_end() -> None:
    """universe 内但 ohlcv 没有 amount 的 symbol → amount=0 → 排到末尾。"""
    ohlcv = _make_amounts({"X": [100.0] * 20})
    out = build_portfolio_universe(
        full_universe_symbols=["X", "GHOST"], ohlcv=ohlcv, top_n=2, window=20
    )
    assert out[0] == "X"
    assert "GHOST" in out


def test_all_zero_amount_falls_back_to_dict_order() -> None:
    ohlcv = _make_amounts({"Z": [0.0] * 20, "A": [0.0] * 20})
    out = build_portfolio_universe(
        full_universe_symbols=["Z", "A", "M"], ohlcv=ohlcv, top_n=2, window=20
    )
    # 字典序：A < M < Z
    assert out == ["A", "M"]


def test_empty_universe_returns_empty() -> None:
    out = build_portfolio_universe(full_universe_symbols=[], ohlcv=pd.DataFrame(), top_n=10, window=20)
    assert out == []


def test_empty_ohlcv_falls_back_to_dict_order() -> None:
    out = build_portfolio_universe(
        full_universe_symbols=["B", "C", "A"],
        ohlcv=pd.DataFrame(),
        top_n=2,
        window=20,
    )
    assert out == ["A", "B"]
