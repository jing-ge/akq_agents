"""P3a Preprocessor 简化版：MAD 去极值 + z-score 横截面 + direction 反号。

P3b 起：增加行业 + 市值中性化（OLS 残差）。P3a 不做。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize_mad(s: pd.Series, k: float = 3.0) -> pd.Series:
    """MAD 去极值：以 median ± k * MAD 截断。

    MAD = median(|x - median(x)|)
    截断阈值 = median ± k * 1.4826 * MAD（1.4826 是与正态分布 std 对齐的系数）
    """
    if s.empty or s.isna().all():
        return s
    median = s.median(skipna=True)
    mad = (s - median).abs().median(skipna=True)
    if mad == 0:
        return s
    threshold = k * 1.4826 * mad
    return s.clip(lower=median - threshold, upper=median + threshold)


def zscore(s: pd.Series) -> pd.Series:
    """横截面 z-score。std=0 时返回全 0。"""
    if s.empty or s.isna().all():
        return s
    mean = s.mean(skipna=True)
    std = float(s.std(skipna=True, ddof=1) or 0.0)
    if std == 0.0 or pd.isna(std):
        return pd.Series(0.0, index=s.index, name=s.name)
    return (s - mean) / std


class Preprocessor:
    """P3a 简化版：winsorize + zscore + direction 反号。

    Args:
        winsorize_k: MAD 去极值的倍数（默认 3）
    """

    def __init__(self, winsorize_k: float = 3.0) -> None:
        self._k = winsorize_k

    def transform(
        self,
        factor_df: pd.DataFrame,
        directions: dict[str, str],
    ) -> pd.DataFrame:
        """对每个因子列：winsorize → zscore → 若 direction='short' 则反号。

        Args:
            factor_df: index=symbol, columns=factor_name 的 wide DataFrame
            directions: {factor_name: 'long' | 'short'}

        Returns:
            同样形状的 wide DataFrame，但 values 已标准化、direction 已统一为"long"（越大越好）
        """
        if factor_df.empty:
            return factor_df
        out_cols: dict[str, pd.Series] = {}
        for col in factor_df.columns:
            s = pd.Series(factor_df[col])
            s = winsorize_mad(s, k=self._k)
            s = zscore(s)
            direction = directions.get(str(col), "long")
            if direction == "short":
                s = -s
            # zscore 全 0 / 全 NaN 时保留；下游 CompositeScorer 会忽略
            out_cols[str(col)] = s.replace([np.inf, -np.inf], np.nan)
        out = pd.DataFrame(out_cols)
        out.index = factor_df.index
        return out
