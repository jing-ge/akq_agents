"""QualityGate 单元测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from akq_agents.models.data_config import QualityConfig
from akq_agents.services.data.exceptions import QualityCheckFailed
from akq_agents.services.data.quality import QualityGate


def _make_frame(n: int, close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"{i:06d}" for i in range(n)],
            "close": [close] * n,
            "volume": [1000.0] * n,
            "amount": [10000.0] * n,
        }
    )


def _config(**overrides) -> QualityConfig:
    base = {
        "min_universe_size": 3,
        "max_null_rate": 0.01,
        "min_close": 0.5,
        "max_close": 2000.0,
    }
    base.update(overrides)
    return QualityConfig(**base)


def test_passes_when_all_three_checks_pass() -> None:
    gate = QualityGate(_config())
    result = gate.check(_make_frame(5))
    assert result == {"row_count": True, "null_rate": True, "close_range": True}


def test_row_count_below_threshold_raises() -> None:
    gate = QualityGate(_config(min_universe_size=10))
    with pytest.raises(QualityCheckFailed) as excinfo:
        gate.check(_make_frame(5))
    assert excinfo.value.checks["row_count"] is False


def test_too_many_nulls_in_required_column_raises() -> None:
    df = _make_frame(100)
    df.loc[:50, "close"] = None  # ~50% null
    with pytest.raises(QualityCheckFailed) as excinfo:
        QualityGate(_config(min_universe_size=10)).check(df)
    assert excinfo.value.checks["null_rate"] is False


def test_missing_required_column_raises() -> None:
    df = _make_frame(100).drop(columns=["amount"])
    with pytest.raises(QualityCheckFailed) as excinfo:
        QualityGate(_config(min_universe_size=10)).check(df)
    assert excinfo.value.checks["null_rate"] is False


def test_close_below_min_raises() -> None:
    """全员低于 min_close → fail（大面积异常）。"""
    df = _make_frame(100, close=0.1)
    with pytest.raises(QualityCheckFailed) as excinfo:
        QualityGate(_config(min_universe_size=10)).check(df)
    assert excinfo.value.checks["close_range"] is False


def test_close_above_max_raises() -> None:
    """全员高于 max_close → fail（大面积异常）。"""
    df = _make_frame(100, close=9999.0)
    with pytest.raises(QualityCheckFailed) as excinfo:
        QualityGate(_config(min_universe_size=10)).check(df)
    assert excinfo.value.checks["close_range"] is False


def test_close_range_tolerates_small_fraction_of_outliers() -> None:
    """少量低价股（如仙股 0.16）不应让整个 quality 失败。

    阈值与 ``max_null_rate`` 对齐：默认 1%。100 行里 1 行越界仍 pass。
    """
    df = _make_frame(100, close=10.0)
    df.loc[0, "close"] = 0.16  # 1% 越界
    gate = QualityGate(_config(min_universe_size=10, max_null_rate=0.02))
    result = gate.check(df)
    assert result["close_range"] is True


def test_close_range_fails_when_outliers_exceed_threshold() -> None:
    """越界比例 > max_null_rate 时仍 fail。"""
    df = _make_frame(100, close=10.0)
    df.loc[:10, "close"] = 0.1  # ~10% 越界
    with pytest.raises(QualityCheckFailed) as excinfo:
        QualityGate(_config(min_universe_size=10, max_null_rate=0.01)).check(df)
    assert excinfo.value.checks["close_range"] is False


def test_empty_frame_fails_all() -> None:
    gate = QualityGate(_config(min_universe_size=1))
    with pytest.raises(QualityCheckFailed) as excinfo:
        gate.check(pd.DataFrame())
    assert excinfo.value.checks["row_count"] is False
