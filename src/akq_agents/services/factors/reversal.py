"""反转因子 Reversal5。

公式：- (close[t] / close[t-5] - 1)
方向：long（因为我们已经在内部反号，输出"越大越好" = 过去 5 日跌得越多越好）

设计选择：direction='long' + 内部反号，与 spec §4 Factor 协议要求一致。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from akq_agents.services.factors.momentum import _pivot_close


@dataclass
class _Reversal5:
    name: str = "reversal_5"
    factor_version: int = 1
    lookback_days: int = 10
    window: int = 5
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
        # 反号：过去跌得越多（ratio 越负） → 反号后越大
        reversal = -(last / past - 1.0)
        reversal.name = self.name
        return reversal.replace([np.inf, -np.inf], np.nan)


def reversal_5() -> _Reversal5:
    return _Reversal5()
