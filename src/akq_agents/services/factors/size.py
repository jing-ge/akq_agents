"""LogAmount：规模代理（避免依赖财务数据获取真实 market_cap）。

P3a 简化方案：用 ``log(amount_20_mean)`` 作为"流动性 + 规模"复合代理。这是个
妥协选择 —— 真正的 market_cap 需要财务数据（P1.5 范畴）。spec §1 已说明 P3a 不
做财务因子；用 log(amount) 是常见替代。

方向：short（小盘股长期看溢价更高；越小越好）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class _LogAmount:
    name: str = "log_amount_20"
    factor_version: int = 1
    lookback_days: int = 20
    window: int = 20
    direction: str = "short"
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
        # log1p 避免 log(0) = -inf
        log_mean = np.log1p(mean)
        log_mean.name = self.name
        return log_mean.replace([np.inf, -np.inf], np.nan)


def log_amount_20() -> _LogAmount:
    return _LogAmount()
