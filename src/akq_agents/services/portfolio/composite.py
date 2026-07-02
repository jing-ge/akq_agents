"""CompositeScorer：把多个标准化后的因子合成一个综合分。

支持两种加权模式：
- ``equal``: 等权（默认 fallback）；
- ``ir``: 按因子最近一次 |IR| 加权（取 max(IR, 0) 归一化；负 IR 视为 0）。

M19: 加 ``min_abs_ir`` 阈值 (默认 0.10), 低于阈值的因子权重直接 0 — 让 builtin/auto/llm
来源不同但表现差的因子统一退场。配合 ``restore_accepted_factors`` 也带 shadow 因子,
表现达标的 shadow LLM 因子会立即参与今日组合, 不必等 20 天 OOS promote。
"""

from __future__ import annotations

from typing import Any

import pandas as pd


class CompositeScorer:
    """因子合成器。"""

    def __init__(
        self,
        weighting: str = "equal",
        evaluator: Any | None = None,
        *,
        min_abs_ir: float = 0.10,
    ) -> None:
        if weighting not in {"equal", "ir"}:
            raise ValueError(f"weighting must be 'equal' or 'ir', got {weighting!r}")
        self._weighting = weighting
        self._evaluator = evaluator
        self._min_abs_ir = float(min_abs_ir)
        self._last_weights: pd.Series = pd.Series(dtype=float)

    def score(self, factor_df: pd.DataFrame, *, as_of_date: Any = None) -> pd.Series:
        """计算综合分。

        Args:
            factor_df: index=symbol, columns=factor_name, values=已标准化的因子值
            as_of_date: 可选 date / ISO 字符串。如指定且 weighting='ir', IR-EWMA
                只用 as_of_date 之前的历史 metrics 计算权重 (M19 修 lookahead bias —
                历史回填时避免用未来 IR 给历史回测加权)。

        Returns:
            index=symbol, values=composite_score 的 Series
        """
        if factor_df.empty:
            self._last_weights = pd.Series(dtype=float)
            return pd.Series(dtype=float, name="composite_score")
        weights = self._compute_weights(list(factor_df.columns), as_of_date=as_of_date)
        self._last_weights = weights
        # 按列加权 mean（NaN→0 不贡献）
        weighted = factor_df.fillna(0.0).mul(weights, axis=1)
        composite = pd.Series(weighted.sum(axis=1))
        composite.name = "composite_score"
        return composite

    def factor_weights(self) -> pd.Series:
        """返回上次 score 时使用的因子权重（用于 Attributor）。"""
        return self._last_weights.copy()

    def compute_weights_for(self, factor_names: list[str], *, as_of_date: Any = None) -> pd.Series:
        """对外暴露: 给定因子名列表, 返回它们在 IR-EWMA 加权下的权重 (不需要先跑 score).

        用于 UI 展示 "如果这些因子参与组合, 各自权重多少". 不修改 _last_weights。
        """
        return self._compute_weights(factor_names, as_of_date=as_of_date)

    # ------------------------------------------------------------------

    def _compute_weights(self, factor_names: list[str], *, as_of_date: Any = None) -> pd.Series:
        n = len(factor_names)
        if self._weighting == "equal" or self._evaluator is None:
            return pd.Series(1.0 / n, index=factor_names, dtype=float)

        # M19: as_of_date 非空时, list_history 用 as_of_filter 限制只看历史. 用于回填.
        as_of_filter = None
        if as_of_date is not None:
            if hasattr(as_of_date, "isoformat"):
                as_of_filter = as_of_date.isoformat()
            else:
                as_of_filter = str(as_of_date)

        # M7-C: 取每个因子最近 N 期历史 IR 做 EWMA（半衰期 30 天，权重更倾向近期）
        # fallback：缺历史则用 latest |IR|
        irs: dict[str, float] = {}
        for name in factor_names:
            try:
                history = (
                    self._evaluator.list_history(name, limit=120, as_of_filter=as_of_filter)
                    if hasattr(self._evaluator, "list_history") else []
                )
            except Exception:
                history = []
            ir_value = self._ewma_abs_ir(history)
            if ir_value is None:
                # M18-I4: 退化用 latest — 之前 get_latest(name, 1) 把 1 当 factor_version,
                # 任何因子升级到 v2 后这条 fallback 立即返回 None。改用 list_history(limit=1)
                # 不依赖 factor_version。
                m = None
                try:
                    latest_list = (
                        self._evaluator.list_history(name, limit=1, as_of_filter=as_of_filter)
                        if hasattr(self._evaluator, "list_history") else []
                    )
                    if latest_list:
                        m = latest_list[0]
                except Exception:
                    m = None
                if m is not None and m.ir is not None:
                    ir_value = max(float(m.ir), 0.0)
            irs[name] = ir_value if ir_value is not None else -1.0

        # M19: 公平筛选 — builtin/auto/llm 不分来源, 用 min_abs_ir 阈值过滤
        # (默认 0.10). missing IR 视作 0 (不参与组合); 之前用 median 兜底是为了让
        # "新因子有机会"补 IR, 但加了入库 90 天 backfill 后这个保护过时了 —
        # 真"missing"的因子说明 backfill 也算不出 IC, 不该硬塞。
        for name, v in list(irs.items()):
            if v < self._min_abs_ir:  # missing (-1.0) 或低于阈值
                irs[name] = 0.0

        total = sum(irs.values())
        if total <= 0:
            # 全部因子都未达阈值: 退到等权 (兜底, 至少能出组合)
            return pd.Series(1.0 / n, index=factor_names, dtype=float)
        return pd.Series({name: v / total for name, v in irs.items()}, dtype=float)

    @staticmethod
    def _ewma_abs_ir(history: list) -> float | None:
        """EWMA half-life=30 天的 |IR|，对负 IR 截断到 0（与 fallback 路径一致）。

        若长期 IR 为负 (反向预测能力)，应当不再贡献组合权重，而非用 |IR| 当成"还不错"。
        和 line 95 的 ``max(float(m.ir), 0.0)`` 保持一致的语义。
        """
        if not history:
            return None
        # 取最近 90 条
        sub = history[:90]
        irs = [max(float(m.ir), 0.0) for m in sub if getattr(m, "ir", None) is not None]
        if not irs:
            return None
        # EWMA: 权重 = 0.5 ** (i / 30)，i=0 表示最新
        import math
        weights = [math.pow(0.5, i / 30.0) for i in range(len(irs))]
        wsum = sum(weights)
        return sum(w * v for w, v in zip(weights, irs, strict=False)) / wsum if wsum > 0 else None
