"""Amount20Rank：过去 20 日成交额均值（流动性代理）。

公式：mean(amount[t-20:t])
方向：long（成交额越大流动性越好）

注：通常会做 rank 处理，但 Preprocessor 阶段已经做了 z-score 横截面标准化，
这里只输出 raw mean 即可，下游 Preprocessor 负责把它压到可比的尺度。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class _Amount20Rank:
    name: str = "amount_20"
    factor_version: int = 1
    lookback_days: int = 20
    window: int = 20
    direction: str = "long"
    inputs: tuple[str, ...] = ("ohlcv",)

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        if ohlcv.empty:
            return pd.Series(dtype=float, name=self.name)
        amount = ohlcv.pivot_table(
            index="date", columns="symbol", values="amount", aggfunc="last"
        ).sort_index()
        if len(amount) < 1:
            return pd.Series(dtype=float, name=self.name)
        tail = amount.iloc[-self.window:]
        mean = tail.mean(axis=0, skipna=True)
        mean.name = self.name
        return mean.replace([np.inf, -np.inf], np.nan)


def amount_20() -> _Amount20Rank:
    return _Amount20Rank()
