"""CompositeScorer + Optimizer + Attributor 测试，含 A6 (归因等式) 验收。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.portfolio.attributor import Attributor
from akq_agents.services.portfolio.composite import CompositeScorer
from akq_agents.services.portfolio.optimizer import OptimizerConfig, PortfolioOptimizer

# -------------------- CompositeScorer --------------------


def test_composite_scorer_equal_weight_mean() -> None:
    df = pd.DataFrame(
        {"momentum_5": [1.0, 2.0, 3.0], "volatility_20": [2.0, 4.0, 6.0]},
        index=["A", "B", "C"],
    )
    scorer = CompositeScorer()
    out = scorer.score(df)
    # equal weight: (col1 + col2) / 2
    assert out["A"] == pytest.approx(1.5)
    assert out["B"] == pytest.approx(3.0)
    assert out["C"] == pytest.approx(4.5)
    assert scorer.factor_weights().to_dict() == {"momentum_5": 0.5, "volatility_20": 0.5}


def test_composite_scorer_nan_treated_as_zero() -> None:
    df = pd.DataFrame(
        {"f1": [1.0, np.nan, 3.0], "f2": [2.0, 4.0, np.nan]},
        index=["A", "B", "C"],
    )
    out = CompositeScorer().score(df)
    assert out["B"] == pytest.approx(2.0)  # (0+4)/2 = 2  (NaN → 0)
    assert out["C"] == pytest.approx(1.5)  # (3+0)/2 = 1.5


def test_composite_scorer_empty() -> None:
    out = CompositeScorer().score(pd.DataFrame())
    assert out.empty


def test_composite_scorer_unknown_weighting_raises() -> None:
    # 早期 P3a 只允许 'equal', 'ir' 也报错; M7-C 起 'ir' 已实现 → 测试改成真"未知"值。
    # 错误信息从 "P3a only supports..." 改成列出合法值 "must be 'equal' or 'ir'", 这里只匹配关键词。
    with pytest.raises(ValueError, match="weighting"):
        CompositeScorer(weighting="unknown_strategy")


# -------------------- PortfolioOptimizer --------------------


def test_optimizer_top_n_inverse_vol_sum_to_one() -> None:
    scores = pd.Series({"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5, "E": 0.0})
    vols = pd.Series({"A": 0.02, "B": 0.01, "C": 0.05, "D": 0.03, "E": 0.04})
    cfg = OptimizerConfig(top_n=3, max_single_weight=1.0)
    w = PortfolioOptimizer(cfg).solve(scores, vols)
    # top 3 by score: A, B, C
    assert set(w.index) == {"A", "B", "C"}
    assert w.sum() == pytest.approx(1.0)
    # B 的 vol 最低 → weight 最高
    assert w["B"] > w["A"] > w["C"]


def test_optimizer_max_single_weight_cap() -> None:
    """单股权重不能超过 max_single_weight。"""
    scores = pd.Series({"A": 1.0, "B": 0.9, "C": 0.8, "D": 0.7})
    # A 的 vol 极小 → inverse-vol 给它 ~80% 权重
    vols = pd.Series({"A": 0.001, "B": 0.05, "C": 0.05, "D": 0.05})
    cfg = OptimizerConfig(top_n=4, max_single_weight=0.4)
    w = PortfolioOptimizer(cfg).solve(scores, vols)
    assert all(w <= 0.4 + 1e-9)
    assert w.sum() == pytest.approx(1.0)


def test_optimizer_rejects_zero_vol_symbols() -> None:
    """vol < min_vol 的 symbol（疑似停牌）被剔除。"""
    scores = pd.Series({"SUSPENDED": 10.0, "OK": 1.0})
    vols = pd.Series({"SUSPENDED": 0.0, "OK": 0.05})
    w = PortfolioOptimizer(OptimizerConfig(top_n=10)).solve(scores, vols)
    assert "SUSPENDED" not in w.index
    assert "OK" in w.index


def test_optimizer_handles_empty() -> None:
    w = PortfolioOptimizer().solve(pd.Series(dtype=float), pd.Series(dtype=float))
    assert w.empty


def test_optimizer_drops_nan_score_symbols() -> None:
    scores = pd.Series({"A": 1.0, "B": np.nan, "C": 0.5})
    vols = pd.Series({"A": 0.02, "B": 0.02, "C": 0.02})
    w = PortfolioOptimizer(OptimizerConfig(top_n=10)).solve(scores, vols)
    assert "B" not in w.index


# -------------------- Attributor & A6 验收 --------------------


def test_attribution_sum_equals_composite_a6() -> None:
    """A6 验收：|Σ_f contribution_{s,f} − composite_score_s| < 1e-6 for all s。"""
    rng = np.random.default_rng(2026)
    symbols = [f"S{i:03d}" for i in range(50)]
    factors = ["f1", "f2", "f3"]
    factor_z = pd.DataFrame(rng.normal(0, 1, (50, 3)), index=symbols, columns=factors)

    scorer = CompositeScorer()
    composite = scorer.score(factor_z)
    weights = pd.Series([0.01] * 50, index=symbols)  # 不影响等式（per-stock 验证用 contrib 之和）

    attr = Attributor().explain(
        weights=weights,
        factor_z=factor_z,
        factor_weights=scorer.factor_weights(),
        as_of_date=date(2026, 6, 17),
    )
    # 重新拼回每股的 contribution 之和
    for sym, factors_list in attr.per_stock.items():
        total = sum(item["contribution"] for item in factors_list)
        # top_k = 5 但我们只有 3 个因子，所以 sum = composite
        assert abs(total - composite[sym]) < 1e-6, f"{sym}: {total} vs {composite[sym]}"


def test_attribution_portfolio_contribution_sums_to_weighted_composite() -> None:
    """组合层归因总和等于 sum(W_s × comp_s)。"""
    rng = np.random.default_rng(7)
    symbols = [f"S{i}" for i in range(10)]
    factors = ["f1", "f2"]
    factor_z = pd.DataFrame(rng.normal(0, 1, (10, 2)), index=symbols, columns=factors)
    scorer = CompositeScorer()
    composite = scorer.score(factor_z)
    weights = pd.Series([1.0 / 10] * 10, index=symbols)
    attr = Attributor().explain(
        weights=weights,
        factor_z=factor_z,
        factor_weights=scorer.factor_weights(),
        as_of_date=date(2026, 6, 17),
    )
    expected = float((weights * composite).sum())
    got = sum(attr.portfolio_contribution.values())
    assert abs(got - expected) < 1e-6


def test_attribution_top_k_per_stock() -> None:
    factor_z = pd.DataFrame(
        {"f1": [1.0], "f2": [2.0], "f3": [3.0], "f4": [4.0], "f5": [5.0], "f6": [6.0]},
        index=["A"],
    )
    scorer = CompositeScorer()
    scorer.score(factor_z)
    attr = Attributor().explain(
        weights=pd.Series({"A": 1.0}),
        factor_z=factor_z,
        factor_weights=scorer.factor_weights(),
        as_of_date=date(2026, 6, 17),
        top_k=3,
    )
    assert len(attr.per_stock["A"]) == 3
    # |contribution| 最大的应在最前；f6 × (1/6) 最大
    assert attr.per_stock["A"][0]["name"] == "f6"


def test_attribution_empty_portfolio() -> None:
    attr = Attributor().explain(
        weights=pd.Series(dtype=float),
        factor_z=pd.DataFrame(),
        factor_weights=pd.Series(dtype=float),
        as_of_date=date(2026, 6, 17),
    )
    assert attr.portfolio_contribution == {}
    assert attr.per_stock == {}
    assert "空" in attr.summary
