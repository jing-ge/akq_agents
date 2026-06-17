"""CompositeScorer：把多个标准化后的因子合成一个综合分。

P3a：equal weight（按 direction 反号已在 Preprocessor 完成；这里只做 mean）。
P3b：基于 IR 加权 + 失能闭环。
"""

from __future__ import annotations

import pandas as pd


class CompositeScorer:
    """P3a 等权合成器。"""

    def __init__(self, weighting: str = "equal") -> None:
        if weighting != "equal":
            raise ValueError(f"P3a only supports weighting='equal', got {weighting!r}")
        self._weighting = weighting
        # 缓存上一次使用的因子权重（Attributor 需要）
        self._last_weights: pd.Series = pd.Series(dtype=float)

    def score(self, factor_df: pd.DataFrame) -> pd.Series:
        """计算综合分。

        Args:
            factor_df: index=symbol, columns=factor_name, values=已标准化的因子值

        Returns:
            index=symbol, values=composite_score 的 Series
        """
        if factor_df.empty:
            self._last_weights = pd.Series(dtype=float)
            return pd.Series(dtype=float, name="composite_score")
        n = len(factor_df.columns)
        weights = pd.Series(1.0 / n, index=factor_df.columns, dtype=float)
        self._last_weights = weights
        # mean(axis=1) 在 NaN 时自动跳过；fillna(0) 使空因子不贡献
        # 这里我们想 strict 些：只对非全 NaN 的行计算 mean
        composite = pd.Series(factor_df.fillna(0.0).mean(axis=1))
        composite.name = "composite_score"
        return composite

    def factor_weights(self) -> pd.Series:
        """返回上次 score 时使用的因子权重（用于 Attributor）。"""
        return self._last_weights.copy()
