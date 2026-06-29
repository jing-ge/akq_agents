"""M20 review: lookahead bias regression tests.

oracle 报告 P0: 缺少"注入未来信息的因子, 断言 IR ≈ 0 而非高分"的回归测试。
如果哪天 evaluator / discovery / backfill 改出 lookahead 路径, 这些测试会立刻 break。

测试设计:
- 构造合成 close 数据 + 一个**完全随机**的因子值 → IR 应该 ≈ 0 (±0.3 内)
- 构造一个**用未来 close 计算**的因子值 (lookahead) → 如果系统泄漏, IR 会 ≈ 1.0
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.portfolio.evaluator import _rolling_ic


def _make_synthetic_close(n_days: int = 120, n_syms: int = 30, seed: int = 42) -> pd.DataFrame:
    """构造确定性合成 close: random walk."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n_days, freq="B")
    syms = [f"S{i:03d}" for i in range(n_syms)]
    # log returns 服从 N(0, 0.02), 起始价 100
    log_rets = rng.normal(0, 0.02, size=(n_days, n_syms))
    log_rets[0] = 0
    prices = 100.0 * np.exp(log_rets.cumsum(axis=0))
    return pd.DataFrame(prices, index=dates, columns=syms)


def test_random_factor_should_have_low_ir() -> None:
    """无信号因子 (纯随机) 的 IR 应该接近 0 (|IR| < 0.3 in 90-day window)。

    如果 IR 高, 说明 evaluator 算法有问题 (用了未来信息 / 配对错位 / 等等)。
    """
    close = _make_synthetic_close(n_days=120, n_syms=30, seed=42)
    forward_returns = close.pct_change(fill_method=None).shift(-1)

    rng = np.random.default_rng(seed=999)  # 独立 seed, 跟 close 完全无关
    factor_history = pd.DataFrame(
        rng.normal(0, 1, size=close.shape),
        index=close.index, columns=close.columns,
    )

    ic = _rolling_ic(factor_history, forward_returns, window=90).dropna()
    assert len(ic) >= 50, f"测试期 90 天 rolling 应有 ≥50 个有效 IC, 实际 {len(ic)}"
    ic_mean = float(ic.mean())
    ic_std = float(ic.std(ddof=1))
    ir = ic_mean / ic_std if ic_std > 0 else 0
    assert abs(ir) < 0.3, (
        f"❌ 随机因子 IR={ir:.3f} 异常高 (期望 <0.3)。"
        f" 可能 evaluator 有 lookahead/配对 bug — ic_mean={ic_mean:.4f}, ic_std={ic_std:.4f}"
    )


def test_lookahead_factor_should_have_perfect_ir() -> None:
    """❗ Lookahead 故意泄漏: 因子值 = 下一日 return (perfect foresight) → IR 必然 ≈ 1.

    这条测试**反向**保护 — 验证测试框架本身能识别 lookahead. 如果有天这个测试也
    失败 (IR < 0.5), 说明 evaluator 把配对算错了。
    """
    close = _make_synthetic_close(n_days=120, n_syms=30, seed=7)
    forward_returns = close.pct_change(fill_method=None).shift(-1)

    # 故意泄漏: 因子 = forward_returns (即"明天涨多少", today 不可能知道)
    factor_history = forward_returns.copy()

    ic = _rolling_ic(factor_history, forward_returns, window=60).dropna()
    assert len(ic) >= 30, f"应有 ≥30 个 IC, 实际 {len(ic)}"
    ic_mean = float(ic.mean())
    # 完美预测时 spearman rank corr 应该 = 1
    assert ic_mean > 0.95, (
        f"❌ Lookahead 测试: ic_mean={ic_mean:.4f} 应 >0.95 (perfect foresight)。"
        f" 如果失败说明 forward_returns / factor_history 的日期对齐 / shift 有 bug, "
        f"导致 evaluator 无法识别 lookahead, 真实生产因子可能 silent 泄漏未来信息."
    )


def test_lagged_factor_should_have_zero_ir() -> None:
    """🔍 Lagged 因子 (因子 = 昨日 return 的反信号) 应该跟未来无关 → IR ≈ 0。

    构造: factor[t] = -return[t-1] (过去反转策略, 但对未来 t+1 无关)
    """
    close = _make_synthetic_close(n_days=120, n_syms=30, seed=11)
    daily_returns = close.pct_change(fill_method=None)
    forward_returns = close.pct_change(fill_method=None).shift(-1)

    factor_history = -daily_returns.shift(1)  # t 时刻的因子 = -t-1 return
    # 注: random walk 下 return[t-1] 和 return[t+1] 独立, 所以 IC 应 ≈ 0

    ic = _rolling_ic(factor_history, forward_returns, window=60).dropna()
    if len(ic) < 30:
        pytest.skip(f"too few IC samples: {len(ic)}")
    ic_mean = float(ic.mean())
    ic_std = float(ic.std(ddof=1))
    ir = ic_mean / ic_std if ic_std > 0 else 0
    assert abs(ir) < 0.5, (
        f"❌ Lagged 反转因子 IR={ir:.3f} 异常高 (期望 <0.5)。"
        f" random walk 下昨日 return 跟明日 return 应该独立, 高 IR 暗示数据生成 / "
        f"evaluator 算法有问题."
    )


def test_evaluator_pairs_factor_t_with_return_t_plus_1() -> None:
    """验证 evaluator 把 factor[t] 跟 return[t+1] 配对 (而不是 t 自己 / t-1)。

    构造一个**只有最后一天有信号**的因子, 看 IC 序列哪天非零。
    """
    n_days = 30
    n_syms = 10
    rng = np.random.default_rng(seed=33)
    dates = pd.date_range("2025-01-01", periods=n_days, freq="B")
    syms = [f"S{i:02d}" for i in range(n_syms)]
    # 构造 forward_returns 直接已知, 倒推 close
    fr_values = rng.normal(0, 0.02, size=(n_days, n_syms))
    forward_returns = pd.DataFrame(fr_values, index=dates, columns=syms)

    # factor[t] 默认 0, 只有第 15 天有信号 (= 第 16 天的 fr[15])
    # 如果配对正确, IC[15] ≈ 1, 其他天 ≈ 0
    signal_day = 15
    factor_history = pd.DataFrame(0.0, index=dates, columns=syms)
    factor_history.iloc[signal_day] = fr_values[signal_day]  # 因子 = 当日 forward_return

    ic = _rolling_ic(factor_history, forward_returns, window=n_days)
    ic_at_signal = ic.iloc[signal_day]
    # 应该 ≈ 1 (signal_day 的 factor = forward_return[signal_day], rank corr 完美)
    assert not pd.isna(ic_at_signal), "signal_day IC 不应该是 NaN"
    assert ic_at_signal > 0.95, (
        f"❌ 配对错位: signal_day={signal_day} 的 IC={ic_at_signal:.4f} 应 >0.95. "
        "evaluator 可能没把 factor[t] 跟 forward_return[t] (=close[t+1]-close[t]) 正确配对."
    )
