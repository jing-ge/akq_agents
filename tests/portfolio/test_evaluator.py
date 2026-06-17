"""FactorEvaluator 单元测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from akq_agents.services.factors.momentum import momentum_5
from akq_agents.services.portfolio.evaluator import FactorEvaluator, _rolling_ic


def _make_history(days: int, symbols: int, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成 (factor_history, forward_returns) 测试数据。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=days)
    syms = [f"S{i:03d}" for i in range(symbols)]
    factor = pd.DataFrame(rng.normal(0, 1, (days, symbols)), index=dates, columns=syms)
    # forward_returns 与 factor 正相关（IC > 0）
    rets = factor * 0.05 + rng.normal(0, 0.1, (days, symbols))
    return factor, rets


def test_rolling_ic_positive_when_correlated() -> None:
    factor, rets = _make_history(60, 20)
    ic = _rolling_ic(factor, rets, window=60)
    # IC 序列应主要为正
    assert ic.mean() > 0


def test_rolling_ic_zero_when_independent() -> None:
    rng = np.random.default_rng(123)
    dates = pd.date_range("2026-01-01", periods=60)
    syms = [f"S{i}" for i in range(10)]
    factor = pd.DataFrame(rng.normal(0, 1, (60, 10)), index=dates, columns=syms)
    rets = pd.DataFrame(rng.normal(0, 1, (60, 10)), index=dates, columns=syms)
    ic = _rolling_ic(factor, rets, window=60)
    # 独立序列 IC 期望 0
    assert abs(ic.mean()) < 0.3


def test_rolling_ic_empty_when_history_too_short() -> None:
    factor, rets = _make_history(10, 5)
    ic = _rolling_ic(factor, rets, window=60)
    assert ic.empty


def test_evaluator_writes_metric(tmp_path: Path) -> None:
    factor, rets = _make_history(70, 30)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    metric = evaluator.evaluate(
        factor=momentum_5(),
        factor_history=factor,
        forward_returns=rets,
        as_of_date=date(2026, 6, 17),
    )
    assert metric.factor_name == "momentum_5"
    assert metric.factor_version == 1
    assert metric.as_of_date == "2026-06-17"
    assert metric.window_days == 60
    assert metric.status == "active"
    assert metric.ic_mean is not None


def test_evaluator_insufficient_data_writes_null_metric(tmp_path: Path) -> None:
    factor, rets = _make_history(3, 5)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    metric = evaluator.evaluate(
        factor=momentum_5(),
        factor_history=factor,
        forward_returns=rets,
        as_of_date=date(2026, 6, 17),
    )
    assert metric.reason == "insufficient_data"
    assert metric.ic_mean is None
    assert metric.status == "active"  # P3a 永远 active


def test_evaluator_upsert_idempotent(tmp_path: Path) -> None:
    factor, rets = _make_history(70, 30)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    evaluator.evaluate(factor=momentum_5(), factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 17))
    # 第二次写同样的 (factor, version, as_of, window) 应 upsert
    evaluator.evaluate(factor=momentum_5(), factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 17))
    metrics = evaluator.list_history("momentum_5", limit=10)
    assert len(metrics) == 1


def test_get_latest_returns_most_recent(tmp_path: Path) -> None:
    factor, rets = _make_history(70, 30)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    evaluator.evaluate(factor=momentum_5(), factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 17))
    evaluator.evaluate(factor=momentum_5(), factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 18))
    latest = evaluator.get_latest("momentum_5", 1)
    assert latest is not None
    assert latest.as_of_date == "2026-06-18"


def test_factor_version_separates_metrics(tmp_path: Path) -> None:
    """同一 factor_name 不同 factor_version 不冲突（spec 附录 B §2 承诺）。"""
    factor, rets = _make_history(70, 30)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    f_v1 = momentum_5()
    f_v2 = momentum_5()
    f_v2.factor_version = 2
    evaluator.evaluate(factor=f_v1, factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 17))
    evaluator.evaluate(factor=f_v2, factor_history=factor, forward_returns=rets,
                       as_of_date=date(2026, 6, 17))
    metrics = evaluator.list_history("momentum_5", limit=10)
    assert len(metrics) == 2
    assert {m.factor_version for m in metrics} == {1, 2}


def test_list_history_orders_version_desc_then_date_desc(tmp_path: Path) -> None:
    factor, rets = _make_history(70, 30)
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    for as_of in [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]:
        evaluator.evaluate(factor=momentum_5(), factor_history=factor, forward_returns=rets,
                           as_of_date=as_of)
    metrics = evaluator.list_history("momentum_5", limit=10)
    # 同 version：as_of_date DESC
    assert [m.as_of_date for m in metrics] == ["2026-06-17", "2026-06-16", "2026-06-15"]


def test_get_latest_returns_none_when_no_metric(tmp_path: Path) -> None:
    evaluator = FactorEvaluator(tmp_path / "meta.db", window=60)
    assert evaluator.get_latest("nonexistent", 1) is None
