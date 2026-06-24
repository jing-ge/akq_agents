"""CompositeScorer：把多个标准化后的因子合成一个综合分。

支持两种加权模式：
- ``equal``: 等权（默认 fallback）；
- ``ir``: 按因子最近一次 |IR| 加权（取 max(IR, 0) 归一化；负 IR 视为 0；
  没有 metric 的因子兜底按 equal share 分配，避免新发现的因子被永远 0 权重）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd


class CompositeScorer:
    """因子合成器。"""

    def __init__(self, weighting: str = "equal", evaluator: Any | None = None) -> None:
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

        # M7-C: 取每个因子最近 N 期历史 IR 做 EWMA（半衰期 30 天，权重更倾向近期）
        # fallback：缺历史则用 latest |IR|
        irs: dict[str, float] = {}
        for name in factor_names:
            try:
                history = self._evaluator.list_history(name, limit=120) if hasattr(self._evaluator, "list_history") else []
            except Exception:
                history = []
            ir_value = self._ewma_abs_ir(history)
            if ir_value is None:
                # M18-I4: 退化用 latest — 之前 get_latest(name, 1) 把 1 当 factor_version,
                # 任何因子升级到 v2 后这条 fallback 立即返回 None。改用 list_history(limit=1)
                # 不依赖 factor_version。
                m = None
                try:
                    latest_list = self._evaluator.list_history(name, limit=1) if hasattr(self._evaluator, "list_history") else []
                    if latest_list:
                        m = latest_list[0]
                except Exception:
                    m = None
                if m is not None and m.ir is not None:
                    ir_value = max(float(m.ir), 0.0)
            irs[name] = ir_value if ir_value is not None else -1.0

        # missing 兜底
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

    @staticmethod
    def _ewma_abs_ir(history: list) -> float | None:
        """EWMA half-life=30 天的 |IR|。history 按时间 DESC（最新在前）。"""
        if not history:
            return None
        # 取最近 90 条
        sub = history[:90]
        irs = [abs(float(m.ir)) for m in sub if getattr(m, "ir", None) is not None]
        if not irs:
            return None
        # EWMA: 权重 = 0.5 ** (i / 30)，i=0 表示最新
        import math
        weights = [math.pow(0.5, i / 30.0) for i in range(len(irs))]
        wsum = sum(weights)
        return sum(w * v for w, v in zip(weights, irs)) / wsum if wsum > 0 else None
