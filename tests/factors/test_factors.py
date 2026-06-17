"""P3 Factors 单元测试：协议、Registry、6 个因子数值、Engine。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.factors import (
    FactorEngine,
    FactorRegistry,
    build_default_registry,
)
from akq_agents.services.factors.liquidity import amount_20
from akq_agents.services.factors.momentum import momentum_5, momentum_20, momentum_60
from akq_agents.services.factors.reversal import reversal_5
from akq_agents.services.factors.size import log_amount_20
from akq_agents.services.factors.volatility import volatility_20

# ---------- fixture ----------


def _make_ohlcv(n_days: int = 70, symbols: tuple[str, ...] = ("A", "B", "C")) -> pd.DataFrame:
    """生成 n_days × len(symbols) 的合成 OHLCV。"""
    rng = np.random.default_rng(42)
    rows = []
    for sym_idx, sym in enumerate(symbols):
        # base price 不同，路径平稳
        base = 10.0 * (sym_idx + 1)
        prices = base * (1 + rng.normal(0, 0.01, n_days)).cumprod()
        for day_idx in range(n_days):
            p = prices[day_idx]
            rows.append(
                {
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=day_idx),
                    "symbol": sym,
                    "open": p * 0.99,
                    "high": p * 1.01,
                    "low": p * 0.98,
                    "close": p,
                    "volume": 1_000_000.0 + rng.normal(0, 50_000),
                    "amount": p * (1_000_000.0 + rng.normal(0, 50_000)),
                }
            )
    return pd.DataFrame(rows)


# ---------- Registry ----------


def test_registry_register_and_get() -> None:
    reg = FactorRegistry()
    f = momentum_5()
    reg.register(f)
    assert reg.get("momentum_5") is f


def test_registry_duplicate_same_version_raises() -> None:
    reg = FactorRegistry()
    reg.register(momentum_5())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(momentum_5())


def test_registry_factor_version_must_be_positive() -> None:
    reg = FactorRegistry()
    bad = momentum_5()
    bad.factor_version = 0
    with pytest.raises(ValueError, match="factor_version"):
        reg.register(bad)


def test_registry_list_active_returns_all_for_p3a() -> None:
    """P3a：list_active 等同于 list_all。"""
    reg = build_default_registry()
    import datetime
    assert reg.list_active(datetime.date(2026, 6, 17)) == reg.list_all()


def test_registry_factor_directions() -> None:
    reg = build_default_registry()
    dirs = reg.factor_directions()
    assert dirs["momentum_5"] == "long"
    assert dirs["volatility_20"] == "short"


def test_default_registry_has_seven_factors() -> None:
    reg = build_default_registry()
    names = {f.name for f in reg.list_all()}
    assert names == {
        "momentum_5",
        "momentum_20",
        "momentum_60",
        "reversal_5",
        "volatility_20",
        "amount_20",
        "log_amount_20",
    }


# ---------- Momentum ----------


def test_momentum_5_value_sign_matches_price_change() -> None:
    """构造 A 单调涨、B 单调跌 → momentum_5 应该 A>0、B<0。"""
    days = 30
    rows = []
    base_date = pd.Timestamp("2026-01-01")
    for i in range(days):
        rows.append({"date": base_date + pd.Timedelta(days=i), "symbol": "A",
                     "open": 0, "high": 0, "low": 0, "close": 10.0 + i * 0.1, "volume": 1.0, "amount": 1.0})
        rows.append({"date": base_date + pd.Timedelta(days=i), "symbol": "B",
                     "open": 0, "high": 0, "low": 0, "close": 20.0 - i * 0.1, "volume": 1.0, "amount": 1.0})
    df = pd.DataFrame(rows)
    s = momentum_5().compute(df)
    assert s["A"] > 0
    assert s["B"] < 0


def test_momentum_returns_nan_when_insufficient_history() -> None:
    df = _make_ohlcv(n_days=3)
    s = momentum_60().compute(df)
    assert s.isna().all()


def test_momentum_handles_empty_input() -> None:
    s = momentum_5().compute(pd.DataFrame())
    assert s.empty


# ---------- Reversal ----------


def test_reversal_5_sign_opposite_to_momentum() -> None:
    """同一段数据，reversal_5 应该和 -momentum_5 同号。"""
    df = _make_ohlcv(n_days=20)
    mom = momentum_5().compute(df)
    rev = reversal_5().compute(df)
    # 数值层面应该完全相反（数值上 |rev + mom| ≈ 0）
    diff = (mom + rev).abs().sum()
    assert diff < 1e-9


# ---------- Volatility ----------


def test_volatility_20_zero_for_constant_price() -> None:
    days = 30
    rows = [
        {"date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i), "symbol": "X",
         "open": 0, "high": 0, "low": 0, "close": 10.0, "volume": 1.0, "amount": 1.0}
        for i in range(days)
    ]
    df = pd.DataFrame(rows)
    s = volatility_20().compute(df)
    assert s["X"] == pytest.approx(0.0, abs=1e-9)


def test_volatility_higher_for_more_volatile_series() -> None:
    days = 40
    rng = np.random.default_rng(7)
    rows = []
    for i in range(days):
        ts = pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)
        # A 低波动，B 高波动
        a = 10.0 + 0.01 * i + rng.normal(0, 0.05)
        b = 10.0 + 0.01 * i + rng.normal(0, 0.5)
        rows.append({"date": ts, "symbol": "A", "open": 0, "high": 0, "low": 0, "close": a, "volume": 1.0, "amount": 1.0})
        rows.append({"date": ts, "symbol": "B", "open": 0, "high": 0, "low": 0, "close": b, "volume": 1.0, "amount": 1.0})
    df = pd.DataFrame(rows)
    s = volatility_20().compute(df)
    assert s["B"] > s["A"]


# ---------- Liquidity (amount_20) ----------


def test_amount_20_mean_matches_window() -> None:
    days = 25
    rows = []
    for i in range(days):
        rows.append({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i), "symbol": "A",
                     "open": 0, "high": 0, "low": 0, "close": 10.0, "volume": 1.0, "amount": float(i + 1)})
    df = pd.DataFrame(rows)
    s = amount_20().compute(df)
    # 取最后 20 天的 amount 均值 = mean(6..25) = (6+25)*20/2 / 20 = 15.5
    expected = sum(range(6, 26)) / 20
    assert s["A"] == pytest.approx(expected)


# ---------- Size (log_amount_20) ----------


def test_log_amount_finite_and_positive_when_amount_positive() -> None:
    df = _make_ohlcv(n_days=30)
    s = log_amount_20().compute(df)
    assert s.notna().all()
    assert (s > 0).all()


# ---------- FactorEngine ----------


def test_engine_compute_returns_wide_dataframe() -> None:
    df = _make_ohlcv(n_days=70, symbols=("A", "B", "C"))
    engine = FactorEngine()
    out = engine.compute(df, [momentum_5(), momentum_20(), volatility_20()])
    assert set(out.columns) == {"momentum_5", "momentum_20", "volatility_20"}
    assert set(out.index) == {"A", "B", "C"}
    assert out.index.name == "symbol"


def test_engine_handles_factor_exception_gracefully() -> None:
    """单个 factor 抛异常 → 返回该列全 NaN，不阻塞其他因子。"""
    df = _make_ohlcv(n_days=20)
    engine = FactorEngine()

    class BadFactor:
        name = "bad"
        factor_version = 1
        inputs = ("ohlcv",)
        lookback_days = 1
        direction = "long"
        def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
            raise RuntimeError("intentional")

    out = engine.compute(df, [BadFactor(), momentum_5()])
    assert "bad" in out.columns and out["bad"].isna().all()
    assert "momentum_5" in out.columns
    # momentum_5 应仍能计算（除非历史不足）
    assert not out["momentum_5"].isna().all() or len(_make_ohlcv(n_days=20)["date"].unique()) <= 5


def test_engine_empty_input() -> None:
    out = FactorEngine().compute(pd.DataFrame(), [momentum_5()])
    assert out.empty


# ---------- end-to-end ----------


def test_default_registry_end_to_end_with_engine() -> None:
    df = _make_ohlcv(n_days=80, symbols=("A", "B", "C", "D"))
    reg = build_default_registry()
    out = FactorEngine().compute(df, reg.list_all())
    assert out.shape == (4, 7)
    assert set(out.columns) == {f.name for f in reg.list_all()}
