"""P-Space 重构: DSL 搜索空间扩到 46×37×12×2=40,848 后的 smoke 测试.

覆盖:
- _BASES / _OPS / _WINDOWS 维度大小
- 每个新增 op 在 _apply_op 都能跑通 (无异常)
- 每个新增 base 在 _BASES 都能跑通 (无异常)
- FactorSpace.sample() 在新空间下不会死循环
- 旧 op/base 仍兼容 (回归)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.factors.discovery import (
    FactorSpace,
    _BASES,
    _DIRECTIONS,
    _OPS,
    _WINDOWS,
    _apply_op,
    make_factor,
)


# ---------- fixture ----------

def _make_wide(n_days: int = 60, n_syms: int = 3, seed: int = 42) -> pd.DataFrame:
    """构造 wide-format DataFrame (index=date, columns=symbol)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    data = rng.normal(loc=10.0, scale=1.0, size=(n_days, n_syms))
    return pd.DataFrame(data, index=dates, columns=[f"S{i}" for i in range(n_syms)])


# ---------- 空间大小 ----------

def test_space_size_at_least_two_orders_of_magnitude() -> None:
    """DSL 空间必须 >= 40000 (2 个数量级, 原始 400 × 100)."""
    expected = len(_BASES) * len(_OPS) * len(_WINDOWS) * len(_DIRECTIONS)
    assert expected >= 40_000, f"DSL 空间不足: {expected} < 40000"
    assert FactorSpace().size() == expected


def test_each_axis_expanded() -> None:
    """每个轴都比原始版本大."""
    assert len(_BASES) >= 30, f"_BASES 至少 30, 实际 {len(_BASES)}"
    assert len(_OPS) >= 30, f"_OPS 至少 30, 实际 {len(_OPS)}"
    assert len(_WINDOWS) >= 10, f"_WINDOWS 至少 10, 实际 {len(_WINDOWS)}"
    assert len(_DIRECTIONS) == 2


# ---------- 旧 op 仍兼容 (回归) ----------

@pytest.mark.parametrize("op", [
    "pct_change", "rolling_mean", "rolling_std", "zscore", "rsi",
    "rolling_skew", "ts_max_norm", "ts_min_norm",
])
def test_legacy_ops_still_work(op: str) -> None:
    wide = _make_wide(n_days=60, n_syms=3)
    out = _apply_op(wide, op, window=10)
    assert out is not None
    assert out.shape == wide.shape


# ---------- 新 op smoke ----------

@pytest.mark.parametrize("op", [
    # 滚动扩展
    "rolling_max", "rolling_min", "rolling_median", "rolling_sum", "rolling_kurt",
    # 动量 / 差分
    "delta", "accel", "rolling_corr_self",
    # 加权 / 平滑
    "ema", "wma", "decay_linear",
    # 横截面 (不依赖 window)
    "cs_zscore", "cs_rank", "pct_rank",
    # 稳健统计
    "mad", "iqr", "range_norm",
    # 分布裁剪 / 变换
    "quantile_clip", "abs", "sign", "log_abs", "sqrt_abs",
    # 趋势归一化
    "ts_mean_norm", "ts_median_norm",
    # 时序 zscore / 缩放
    "rolling_zscore", "rolling_robust_zscore", "rolling_scale",
    # 时序百分位 / 排名
    "rolling_pct_rank", "rolling_argmax_norm",
])
def test_new_ops_run_without_error(op: str) -> None:
    wide = _make_wide(n_days=60, n_syms=3)
    out = _apply_op(wide, op, window=10)
    assert out is not None, f"{op} returned None"
    assert out.shape == wide.shape, f"{op} shape mismatch: {out.shape}"


def test_new_ops_handle_short_window() -> None:
    """window=2 / 3 等短窗口不应让 _apply_op 崩溃.

    注: window > len(wide)-1 时 _apply_op 会返回 None (设计内行为, 防止窗口越界).
    这里只测窗口 ≤ n-1 的情况, 大窗口场景在 test_ops_respect_window_bounds 里覆盖.
    """
    wide = _make_wide(n_days=300, n_syms=3)
    for w in (2, 3, 5, 7, 10, 14, 20, 30, 60, 90, 120, 250):
        for op in ("rolling_mean", "zscore", "delta", "ema", "cs_rank", "abs"):
            out = _apply_op(wide, op, window=w)
            assert out is not None, f"op={op} window={w} returned None"


def test_ops_respect_window_bounds() -> None:
    """_apply_op 在 window > len(wide)-1 时必须返回 None (而非崩/越界)."""
    wide = _make_wide(n_days=30, n_syms=3)
    # window=60 超出 30 长度, 应 None
    for op in ("rolling_mean", "zscore", "delta", "ema", "rolling_max", "rolling_kurt"):
        out = _apply_op(wide, op, window=60)
        assert out is None, f"op={op} window=60 should return None, got {type(out)}"


def test_cs_ops_do_not_depend_on_window() -> None:
    """cs_* 类横截面 op 应当对 window 不敏感 (输出形状一致, 值不严重依赖 w).

    只在小 window 范围内比较 — 窗口 > 长度时 cs_* 也应 None (跟其他 op 一致).
    """
    wide = _make_wide(n_days=120, n_syms=5)
    out_small = _apply_op(wide, "cs_zscore", window=5)
    out_large = _apply_op(wide, "cs_zscore", window=60)
    assert out_small is not None and out_large is not None
    # 严格相等 — cs_zscore 只看横截面 (axis=1), window 完全不影响
    pd.testing.assert_frame_equal(out_small, out_large)


# ---------- 新 base smoke ----------

def test_all_bases_are_callable() -> None:
    """每个 base 都能在 long-format OHLCV 上跑通 (无异常)."""
    rng = np.random.default_rng(42)
    n = 30
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D").repeat(2),
        "symbol": ["A", "B"] * n,
        "open": rng.normal(10, 1, n * 2),
        "high": rng.normal(10.5, 1, n * 2),
        "low": rng.normal(9.5, 1, n * 2),
        "close": rng.normal(10, 1, n * 2),
        "volume": rng.normal(1e6, 1e5, n * 2).clip(min=1),
        "amount": rng.normal(1e7, 1e6, n * 2).clip(min=1),
    })
    for name, fn in _BASES.items():
        out = fn(df)
        assert isinstance(out, pd.Series), f"base {name} did not return Series"
        assert len(out) == len(df), f"base {name} length mismatch"


def test_all_new_bases_produce_valid_wide_table() -> None:
    """新 base + 旧 op 组合后, wide pivot 仍能正常生成 (compute 链路通)."""
    rng = np.random.default_rng(7)
    n = 60
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D").repeat(3),
        "symbol": ["A", "B", "C"] * n,
        "open": rng.normal(10, 1, n * 3).clip(min=0.1),
        "high": rng.normal(10.5, 1, n * 3).clip(min=0.1),
        "low": rng.normal(9.5, 1, n * 3).clip(min=0.1),
        "close": rng.normal(10, 1, n * 3).clip(min=0.1),
        "volume": rng.normal(1e6, 1e5, n * 3).clip(min=1),
        "amount": rng.normal(1e7, 1e6, n * 3).clip(min=1),
    })
    # 测 5 个新 base
    new_bases = [
        "log_amount", "turnover", "amihud", "vwap_deviation",
        "hl_amp", "upper_shadow", "lower_shadow", "oc_amp",
        "close_to_vwap", "oc_ret", "overnight_return",
    ]
    for base_name in new_bases:
        if base_name not in _BASES:
            continue
        wide = _BASES[base_name](df).rename("v")
        wide = df[["date", "symbol"]].assign(v=wide).pivot_table(
            index="date", columns="symbol", values="v", aggfunc="last",
        ).sort_index()
        out = _apply_op(wide, "rolling_mean", window=10)
        assert out is not None, f"base={base_name} produce no output"
        assert out.shape == wide.shape


# ---------- sample 不撞重复 ----------

def test_sample_no_duplicates_in_large_space() -> None:
    """新空间下 sample 100 个不撞同 recipe."""
    space = FactorSpace()
    samples = space.sample(100)
    assert len(samples) == 100
    keys = {tuple(sorted(s.items())) for s in samples}
    assert len(keys) == 100


def test_sample_does_not_exceed_space_size() -> None:
    """sample(n) 超过空间大小时, 不会无限循环, 只返回空间大小个."""
    space = FactorSpace()
    samples = space.sample(999_999)
    assert len(samples) == space.size()


# ---------- make_factor 端到端 ----------

def test_make_factor_with_new_recipe() -> None:
    """用新 base + 新 op 构造一个因子, 跑 compute 不报错."""
    recipe = {
        "base": "log_amount", "op": "ema", "window": 20, "direction": "long",
    }
    factor = make_factor(recipe)
    rng = np.random.default_rng(99)
    n = 80
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D").repeat(2),
        "symbol": ["A", "B"] * n,
        "open": rng.normal(10, 1, n * 2).clip(min=0.1),
        "high": rng.normal(10.5, 1, n * 2).clip(min=0.1),
        "low": rng.normal(9.5, 1, n * 2).clip(min=0.1),
        "close": rng.normal(10, 1, n * 2).clip(min=0.1),
        "volume": rng.normal(1e6, 1e5, n * 2).clip(min=1),
        "amount": rng.normal(1e7, 1e6, n * 2).clip(min=1),
    })
    s = factor.compute(df)
    assert s is not None
    assert isinstance(s, pd.Series)
    assert len(s) == 2  # 2 symbols