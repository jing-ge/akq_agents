"""PortfolioOptimizer：inverse-vol top-N + 换手成本惩罚（M7-A）。

算法：
1. 取 composite_score top N（默认 50）
2. 权重 ∝ 1/vol_20
3. 与 prev_weights 比较：把 |delta_w| 过大的部分用 lambda * cost 惩罚
   做法：先算 raw_w，然后对 (raw_w, prev_w) 做线性插值 alpha：
       final_w = alpha * raw_w + (1-alpha) * prev_w（同 symbol）
   其中 alpha ∈ [0, 1] 控制换手强度；alpha=1 完全采纳新权重，alpha=0 完全不动。
   alpha 用 turnover_aversion 配置决定（默认 1.0 即不抑制）。
4. max_single_weight 截断
5. vol < min_vol 视为停牌剔除

这是一个简化的"权重平滑"做法，等价于在目标函数里加 λ|w - w_prev|_1 的近似——
不直接走 cvxpy 是因为 YAGNI：单边成本 0.0008 下，alpha=0.7 即可显著降低换手。
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
    min_vol: float = 1e-4
    # M7-A: 换手抑制系数。1.0 = 完全采纳新权重（无抑制）；0.0 = 完全不动。
    # 建议 0.5-0.8，让组合慢慢往新方向迁移，降低单日换手率。
    turnover_aversion: float = 1.0


class PortfolioOptimizer:
    """inverse-vol top-N + 可选换手抑制。"""

    def __init__(self, cfg: OptimizerConfig | None = None) -> None:
        self._cfg = cfg or OptimizerConfig()

    def solve(
        self,
        composite_score: pd.Series,
        vol_20: pd.Series,
        prev_weights: pd.Series | None = None,
    ) -> pd.Series:
        cfg = self._cfg

        if composite_score.empty:
            return pd.Series(dtype=float, name="weight")

        scored = pd.Series(composite_score).dropna().sort_values(ascending=False)
        vol_aligned = pd.Series(vol_20).reindex(scored.index)
        mask = vol_aligned.notna() & (vol_aligned > cfg.min_vol)
        vol_safe = pd.Series(vol_aligned[mask])
        scored = scored.loc[vol_safe.index]
        top = scored.head(cfg.top_n)
        if top.empty:
            return pd.Series(dtype=float, name="weight")

        vol = pd.Series(vol_safe).reindex(top.index)
        inv_vol = 1.0 / vol
        raw_weights = inv_vol / inv_vol.sum()

        # M7-A: 与 prev_weights 做线性插值降换手
        if (
            prev_weights is not None
            and not prev_weights.empty
            and cfg.turnover_aversion < 1.0
        ):
            alpha = float(cfg.turnover_aversion)
            all_syms = set(raw_weights.index) | set(prev_weights.index)
            mixed = {}
            for s in all_syms:
                rw = float(raw_weights.get(s, 0.0) or 0.0)
                pw = float(prev_weights.get(s, 0.0) or 0.0)
                mixed[s] = alpha * rw + (1 - alpha) * pw
            weights = pd.Series(mixed, dtype=float)
            # 重新归一
            total = weights.sum()
            if total > 0:
                weights = weights / total
            # 把权重 < 0.5% 的小尾巴去掉（避免组合里有太多 prev 残留极小持仓）
            mask_keep = weights > 0.005
            if mask_keep.any():
                weights = pd.Series(weights[mask_keep])
                weights = weights / weights.sum()
            else:
                weights = raw_weights
        else:
            weights = raw_weights

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
