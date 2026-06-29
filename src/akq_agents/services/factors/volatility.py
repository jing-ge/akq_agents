"""Volatility20：过去 20 日日收益率的标准差。

公式：std(returns[t-20:t])，returns = close.pct_change()
方向：short（值越小越好；波动率越低被认为越稳）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from akq_agents.services.factors.momentum import _pivot_close


@dataclass
class _Volatility20:
    name: str = "volatility_20"
    factor_version: int = 1
    lookback_days: int = 30
    window: int = 20
    direction: str = "short"
    inputs: tuple[str, ...] = ("ohlcv",)

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        if ohlcv.empty:
            return pd.Series(dtype=float, name=self.name)
        close = _pivot_close(ohlcv)
        # 至少需要 window+1 天才能算出 window 个收益率
        if len(close) < self.window + 1:
            return pd.Series({sym: np.nan for sym in close.columns}, name=self.name)
        returns = close.pct_change(fill_method=None).iloc[-self.window:]
        vol = returns.std(ddof=1)
        vol.name = self.name
        return vol.replace([np.inf, -np.inf], np.nan)


def volatility_20() -> _Volatility20:
    return _Volatility20()
