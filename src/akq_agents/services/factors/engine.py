"""FactorEngine：批量计算因子。

P3a 简化：串行计算（threadpool 在 4000 标的×6 因子规模下，瓶颈是 pandas pivot 而非
factor 函数本身；不引入线程开销）。如未来证明 IO/CPU 瓶颈，再改为并行。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import pandas as pd

from akq_agents.services.factors.base import Factor

logger = logging.getLogger(__name__)


class FactorEngine:
    """对给定 universe + ohlcv，计算多个 factor 的原始值。"""

    def compute(
        self,
        ohlcv: pd.DataFrame,
        factors: Iterable[Factor],
    ) -> pd.DataFrame:
        """返回 wide DataFrame：index=symbol, columns=factor_name, values=raw_value。

        - 缺数据的 (symbol, factor) 项填 NaN（下游 Preprocessor 会处理）
        - factor.compute 抛异常 → 该列全 NaN + warning log（永不阻塞）
        """
        if ohlcv.empty:
            return pd.DataFrame()
        series_list: list[pd.Series] = []
        for factor in factors:
            try:
                s = factor.compute(ohlcv)
            except Exception:  # noqa: BLE001
                logger.exception("factor %s compute failed", factor.name)
                symbols = ohlcv["symbol"].astype(str).unique()
                s = pd.Series(index=pd.Index(symbols), dtype=float, name=factor.name)
            if s.name != factor.name:
                s = s.rename(factor.name)
            series_list.append(s)
        if not series_list:
            return pd.DataFrame()
        out = pd.concat(series_list, axis=1)
        # 确保 index 是字符串 symbol
        out.index = out.index.astype(str)
        out.index.name = "symbol"
        return out
