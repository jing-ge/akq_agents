"""CompositeScorer：把多个标准化后的因子合成一个综合分。

支持两种加权模式：
- ``equal``: 等权（默认 fallback）；
- ``ir``: 按因子最近一次 |IR| 加权（取 max(IR, 0) 归一化；负 IR 视为 0；
  没有 metric 的因子兜底按 equal share 分配，避免新发现的因子被永远 0 权重）。
"""

from __future__ import annotations

import pandas as pd


class CompositeScorer:
    """因子合成器。"""

    def __init__(self, weighting: str = "equal", evaluator: object | None = None) -> None:
        if weighting not in {"equal", "ir"}:
            raise ValueError(f"weighting must be 'equal' or 'ir', got {weighting!r}")
        self._weighting = weighting
        self._evaluator = evaluator
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
        weights = self._compute_weights(list(factor_df.columns))
        self._last_weights = weights
        # 按列加权 mean（NaN→0 不贡献）
        weighted = factor_df.fillna(0.0).mul(weights, axis=1)
        composite = pd.Series(weighted.sum(axis=1))
        composite.name = "composite_score"
        return composite

    def factor_weights(self) -> pd.Series:
        """返回上次 score 时使用的因子权重（用于 Attributor）。"""
        return self._last_weights.copy()

    # ------------------------------------------------------------------

    def _compute_weights(self, factor_names: list[str]) -> pd.Series:
        n = len(factor_names)
        if self._weighting == "equal" or self._evaluator is None:
            return pd.Series(1.0 / n, index=factor_names, dtype=float)

        # ir 加权：取每个因子最近一次 |IR|，缺失则用集合中位数兜底
        irs: dict[str, float] = {}
        for name in factor_names:
            try:
                m = self._evaluator.get_latest(name, 1)  # type: ignore[attr-defined]
            except Exception:
                m = None
            if m is not None and m.ir is not None:
                irs[name] = max(float(m.ir), 0.0)  # 负 IR 视为 0
            else:
                irs[name] = -1.0  # sentinel for "missing"

        # missing 的兜底：用已有非负 IR 的中位数；若全部缺失 → equal
        valid = [v for v in irs.values() if v >= 0.0]
        if not valid:
            return pd.Series(1.0 / n, index=factor_names, dtype=float)
        median_v = float(pd.Series(valid).median())
        for name, v in list(irs.items()):
            if v < 0:
                irs[name] = max(median_v, 0.0)

        total = sum(irs.values())
        if total <= 0:
            return pd.Series(1.0 / n, index=factor_names, dtype=float)
        return pd.Series({name: v / total for name, v in irs.items()}, dtype=float)
