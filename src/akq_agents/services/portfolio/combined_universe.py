"""CombinedUniverseBuilder：从数据 universe（~5000）筛出组合 universe（top 500 by 流动性）。

P3 spec §1 "v2 收敛说明 §2" 承诺：组合 universe ⊂ 数据 universe，固定取 top 500
by 流动性（20 日成交额均值），避免 4000 维 QP 跑不动。

P3a 没有 cvxpy，但保留该限制有两个原因：
1. 后续 P3b 接 cvxpy 时不需要再改
2. 减少下游 FactorEngine / Preprocessor / Optimizer 的计算量
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_portfolio_universe(
    *,
    full_universe_symbols: list[str],
    ohlcv: pd.DataFrame,
    top_n: int = 500,
    window: int = 20,
) -> list[str]:
    """对 full_universe_symbols 按"过去 window 日成交额均值"排序取 top_n。

    边界：
    - len(full_universe_symbols) <= top_n → 直接返回（按 amount 排序）
    - ohlcv 中缺某 symbol 的 amount → 该 symbol amount 视为 0（被挤到末尾）
    - 全空 amount → 退化为按字典序排序前 top_n + 记 warning（spec §5 风险登记）
    """
    if not full_universe_symbols:
        return []
    if ohlcv.empty:
        logger.warning("build_portfolio_universe: ohlcv empty; fallback to dict-order top %d", top_n)
        return sorted(full_universe_symbols)[:top_n]

    amount = ohlcv.pivot_table(
        index="date", columns="symbol", values="amount", aggfunc="last"
    ).sort_index()
    if len(amount) < 1:
        return sorted(full_universe_symbols)[:top_n]

    tail = amount.iloc[-window:]
    mean = tail.mean(axis=0, skipna=True)
    # 对 universe 内但 ohlcv 缺数据的 symbol 填 0（排到末尾）
    mean = mean.reindex(full_universe_symbols).fillna(0.0)
    mean = mean.replace([np.inf, -np.inf], 0.0)

    if (mean == 0.0).all():
        logger.warning(
            "build_portfolio_universe: all amounts zero/missing; fallback to dict-order top %d", top_n
        )
        return sorted(full_universe_symbols)[:top_n]

    sorted_symbols = mean.sort_values(ascending=False).index.tolist()
    return list(sorted_symbols[:top_n])


def _format_universe_summary(symbols: list[str], full_size: int, as_of: date) -> dict:
    """生成可写入 events 的小摘要。"""
    return {
        "as_of_date": as_of.isoformat(),
        "portfolio_universe_size": len(symbols),
        "full_universe_size": full_size,
    }
