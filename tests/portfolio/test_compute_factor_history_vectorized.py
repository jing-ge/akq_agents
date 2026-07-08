"""Golden 等价回归（方案 1）：向量化 compute_factor_history_vectorized 必须与逐日
_default_compute_factor_history 产出逐位相同的 factor_history。

背景（方案 1 提速）:
- 旧逐日路径: 对 ~150 个交易日每天切 ohlcv[date<=d] 再 factor.compute(sub)，compute 内部
  每次重跑整段 pivot + _apply_op，只取 iloc[-1]（O(N_days × pivot) 重复，占单因子 75-85%）。
- 新向量化: 预 pivot 一次 + 一次 _apply_op 得全历史矩阵，直接返回。
- 等价性依据: _apply_op 的算子都是因果时序算子(rolling/shift/ewm/diff)或按行横截面算子
  (cs_rank/cs_zscore)，d 日值只依赖 d 及之前 + 固定起点 → 全量算取第 d 行 == 逐日算取末行。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.factors.discovery import make_factor
from akq_agents.services.factors.history_backfill import (
    _default_compute_factor_history,
    compute_factor_history_vectorized,
)
from akq_agents.services.factors.proposal_store import recipe_from_json


def _make_ohlcv(n_days: int, n_syms: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n_days).date
    syms = [f"{i:06d}" for i in range(n_syms)]
    rows = []
    price = {s: 10.0 + i for i, s in enumerate(syms)}
    for d in dates:
        for s in syms:
            price[s] *= 1.0 + rng.standard_normal() * 0.02
            p = price[s]
            rows.append({
                "date": d, "symbol": s,
                "open": p, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1e5 + rng.random() * 1e4,
                "amount": p * (1e5 + rng.random() * 1e4),
            })
    df = pd.DataFrame(rows)
    # 插入停牌行 (NaN close) 测 pivot aggfunc / dropna 一致
    df.loc[3, "close"] = np.nan
    return df


def _factor_from_recipe(base: str, op: str, window: int, direction: str = "long"):
    recipe = recipe_from_json(
        f'{{"base":"{base}","op":"{op}","window":{window},"direction":"{direction}"}}'
    )
    return make_factor(recipe)


# 覆盖各类算子: 时序 rolling / 差分 / ewm(记忆型陷阱) / 横截面
_OPS = [
    ("close", "rolling_mean", 10),
    ("close", "rolling_std", 10),
    ("close", "zscore", 10),
    ("close", "rsi", 14),
    ("close", "ema", 10),          # ewm: 记忆型，重点验等价
    ("close", "delta", 5),
    ("close", "cs_rank", 5),       # 横截面按行
    ("close", "cs_zscore", 5),
    ("volume", "rolling_max", 10),
    ("close", "ts_max_norm", 10),
]


@pytest.mark.parametrize("base,op,window", _OPS)
def test_vectorized_equals_daily(base, op, window):
    ohlcv = _make_ohlcv(n_days=150, n_syms=6, seed=hash((base, op, window)) % 10000)
    factor = _factor_from_recipe(base, op, window)
    all_dates = pd.Index(sorted(ohlcv["date"].unique()))

    old = _default_compute_factor_history(factor, ohlcv, all_dates)
    new = compute_factor_history_vectorized(factor, ohlcv, all_dates)

    # 索引对齐 (逐日路径会跳过 lookback 不足 / compute 失败的日期)
    assert set(new.index) == set(old.index), f"index mismatch: {op}"
    old_a, new_a = old.align(new, join="inner")
    # 列对齐
    common_cols = old_a.columns.intersection(new_a.columns)
    assert len(common_cols) > 0
    pd.testing.assert_frame_equal(
        old_a[common_cols].sort_index(),
        new_a[common_cols].sort_index(),
        check_dtype=False, check_names=False, rtol=1e-9, atol=1e-12,
        obj=f"{base}.{op}.{window}",
    )


def test_vectorized_empty_ohlcv():
    factor = _factor_from_recipe("close", "rolling_mean", 10)
    out = compute_factor_history_vectorized(factor, pd.DataFrame(
        columns=["date", "symbol", "close", "volume", "amount", "open", "high", "low"]
    ), pd.Index([]))
    assert out.empty


def test_vectorized_lookback_insufficient():
    """all_dates 前 lookback_days 天应被跳过 (与逐日 continue 一致)。"""
    ohlcv = _make_ohlcv(n_days=30, n_syms=5, seed=7)
    factor = _factor_from_recipe("close", "rolling_mean", 20)
    all_dates = pd.Index(sorted(ohlcv["date"].unique()))
    old = _default_compute_factor_history(factor, ohlcv, all_dates)
    new = compute_factor_history_vectorized(factor, ohlcv, all_dates)
    assert set(new.index) == set(old.index)
