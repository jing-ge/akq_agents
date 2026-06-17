"""Preprocessor 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.portfolio.preprocessor import Preprocessor, winsorize_mad, zscore


def test_winsorize_clips_outliers() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 1000.0])
    out = winsorize_mad(s, k=3.0)
    assert out.iloc[-1] < 1000.0
    assert out.iloc[0] == pytest.approx(1.0)  # 下端不被裁


def test_winsorize_idempotent_on_constant() -> None:
    s = pd.Series([5.0] * 10)
    out = winsorize_mad(s, k=3.0)
    assert (out == 5.0).all()


def test_winsorize_handles_empty() -> None:
    assert winsorize_mad(pd.Series(dtype=float)).empty


def test_zscore_mean_zero_std_one() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    z = zscore(s)
    assert z.mean() == pytest.approx(0.0)
    assert z.std(ddof=1) == pytest.approx(1.0)


def test_zscore_zero_std_returns_zeros() -> None:
    s = pd.Series([5.0] * 5)
    z = zscore(s)
    assert (z == 0.0).all()


def test_preprocessor_long_direction_passthrough() -> None:
    df = pd.DataFrame({"momentum_5": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=["A", "B", "C", "D", "E"])
    pre = Preprocessor()
    out = pre.transform(df, {"momentum_5": "long"})
    # 顺序：原序列升序 → z-score 也升序（最小为 A，最大为 E）
    assert out["momentum_5"].idxmin() == "A"
    assert out["momentum_5"].idxmax() == "E"


def test_preprocessor_short_direction_negates() -> None:
    df = pd.DataFrame({"volatility_20": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=["A", "B", "C", "D", "E"])
    pre = Preprocessor()
    out = pre.transform(df, {"volatility_20": "short"})
    # short 反号后：原最大 E 反号最小，原最小 A 反号最大
    assert out["volatility_20"].idxmin() == "E"
    assert out["volatility_20"].idxmax() == "A"


def test_preprocessor_multiple_factors() -> None:
    df = pd.DataFrame(
        {
            "momentum_5": [1.0, 2.0, 3.0, 4.0, 5.0],
            "volatility_20": [1.0, 2.0, 3.0, 4.0, 5.0],
        },
        index=["A", "B", "C", "D", "E"],
    )
    out = Preprocessor().transform(df, {"momentum_5": "long", "volatility_20": "short"})
    # momentum: A < E； volatility short 反号后 E < A
    assert out.loc["A", "momentum_5"] < out.loc["E", "momentum_5"]
    assert out.loc["A", "volatility_20"] > out.loc["E", "volatility_20"]


def test_preprocessor_winsorizes_extreme_outlier() -> None:
    df = pd.DataFrame(
        {"momentum_5": [1.0, 2.0, 3.0, 4.0, 5.0, 1000.0]},
        index=["A", "B", "C", "D", "E", "F"],
    )
    out = Preprocessor().transform(df, {"momentum_5": "long"})
    # F 是异常值，先 winsorize 截断后 zscore，所以 F 的 z-score 不会异常大
    assert abs(out.loc["F", "momentum_5"]) < 5.0


def test_preprocessor_handles_empty() -> None:
    out = Preprocessor().transform(pd.DataFrame(), {})
    assert out.empty


def test_preprocessor_preserves_index_and_columns() -> None:
    df = pd.DataFrame(
        {"momentum_5": [1.0, 2.0, 3.0], "volatility_20": [4.0, 5.0, 6.0]},
        index=["A", "B", "C"],
    )
    out = Preprocessor().transform(df, {"momentum_5": "long", "volatility_20": "short"})
    assert list(out.index) == ["A", "B", "C"]
    assert set(out.columns) == {"momentum_5", "volatility_20"}


def test_preprocessor_nan_pass_through() -> None:
    df = pd.DataFrame(
        {"momentum_5": [1.0, np.nan, 3.0, 4.0]},
        index=["A", "B", "C", "D"],
    )
    out = Preprocessor().transform(df, {"momentum_5": "long"})
    assert pd.isna(out.loc["B", "momentum_5"])
    # 非 NaN 的 z-score 仍然非 NaN
    assert not pd.isna(out.loc["A", "momentum_5"])
