"""动量因子 Momentum5 / Momentum20 / Momentum60。

公式：close[t] / close[t-N] - 1
方向：long（值越大越好；过去 N 日表现越好越被看好）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def _pivot_close(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """long-format → wide ``index=date, columns=symbol, values=close``。"""
    return ohlcv.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()


@dataclass
class _MomentumBase:
    """共享逻辑：取最后一行 / N 行前一行 → 求 ratio - 1。"""

    name: str
    factor_version: int
    lookback_days: int
    window: int
    direction: str = "long"
    inputs: tuple[str, ...] = ("ohlcv",)

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        if ohlcv.empty:
            return pd.Series(dtype=float, name=self.name)
        close = _pivot_close(ohlcv)
        if len(close) <= self.window:
            return pd.Series({sym: np.nan for sym in close.columns}, name=self.name)
        last = close.iloc[-1]
        past = close.iloc[-1 - self.window]
        ratio = last / past - 1.0
        ratio.name = self.name
        # past 为 0 / NaN 的产生 inf / NaN，统一替换为 NaN
        return ratio.replace([np.inf, -np.inf], np.nan)


def momentum_5() -> _MomentumBase:
    return _MomentumBase(name="momentum_5", factor_version=1, lookback_days=10, window=5)


def momentum_20() -> _MomentumBase:
    return _MomentumBase(name="momentum_20", factor_version=1, lookback_days=30, window=20)


def momentum_60() -> _MomentumBase:
    return _MomentumBase(name="momentum_60", factor_version=1, lookback_days=80, window=60)
