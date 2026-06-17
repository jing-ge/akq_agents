"""PortfolioOptimizer：inverse-vol top-N（P3a 唯一实现）。

算法：
1. 取 composite_score top N（默认 50）
2. 权重 ∝ 1/vol_20，归一化使 sum=1
3. 任一权重 > max_single_weight → 截断，超额按比例分配给其他持仓
4. vol < 1e-4 的 symbol（疑似停牌）直接 reject

P3b 升级：cvxpy mean-variance with constraints；失败 fallback 到当前实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class OptimizerConfig:
    top_n: int = 50
    max_single_weight: float = 0.05
    min_vol: float = 1e-4  # vol 低于此视为停牌，剔除


class PortfolioOptimizer:
    """P3a 简化版：inverse-vol top-N。"""

    def __init__(self, cfg: OptimizerConfig | None = None) -> None:
        self._cfg = cfg or OptimizerConfig()

    def solve(
        self,
        composite_score: pd.Series,
        vol_20: pd.Series,
        prev_weights: pd.Series | None = None,
    ) -> pd.Series:
        """求解 target_weights，返回 index=symbol, values=weight (sum≈1)。"""
        _ = prev_weights  # P3a 不使用；P3b 会用作 turnover 约束
        cfg = self._cfg

        if composite_score.empty:
            return pd.Series(dtype=float, name="weight")

        # 1) 按 score 排序 top N（NaN 默认排到最后，自然被剔除）
        scored = pd.Series(composite_score).dropna().sort_values(ascending=False)
        # 与 vol 取交集，且 vol > min_vol
        vol_aligned = pd.Series(vol_20).reindex(scored.index)
        mask = vol_aligned.notna() & (vol_aligned > cfg.min_vol)
        vol_safe = pd.Series(vol_aligned[mask])
        scored = scored.loc[vol_safe.index]
        # 取 top N
        top = scored.head(cfg.top_n)
        if top.empty:
            return pd.Series(dtype=float, name="weight")

        # 2) inverse-vol 权重
        vol = pd.Series(vol_safe).reindex(top.index)
        inv_vol = 1.0 / vol
        weights = inv_vol / inv_vol.sum()

        # 3) max_single_weight 截断 + 多余按比例转移
        weights = self._cap_and_redistribute(weights, cfg.max_single_weight)

        weights.name = "weight"
        return weights

    @staticmethod
    def _cap_and_redistribute(weights: pd.Series, cap: float) -> pd.Series:
        """对超过 cap 的权重截断，剩余按比例（在未达 cap 的 symbols 上）重新分配。

        最多迭代 5 次（极端情况下可能多次截断都触发 cap）。
        """
        w = weights.copy()
        for _ in range(5):
            over = w > cap
            if not over.any():
                break
            excess = (w[over] - cap).sum()
            w[over] = cap
            under_mask = w < cap
            if not under_mask.any():
                # 全部都 >= cap，无处分配（罕见，意味着 N * cap < 1）
                # 把剩余 excess 均摊在所有 symbol 上
                w = w + excess / len(w)
                break
            # 按当前权重比例分配
            under_sum = w[under_mask].sum()
            if under_sum <= 0:
                # 退化：均摊
                w[under_mask] = w[under_mask] + excess / under_mask.sum()
            else:
                w[under_mask] = w[under_mask] + (w[under_mask] / under_sum) * excess
        # 数值误差归一化
        total = w.sum()
        if total > 0 and abs(total - 1.0) > 1e-9:
            w = w / total
        # 保险：把可能微小超 cap 的截到 cap（数值误差）
        w = pd.Series(np.minimum(w, cap), index=w.index)
        # 再归一化
        total2 = w.sum()
        if total2 > 0:
            w = w / total2
        return w
