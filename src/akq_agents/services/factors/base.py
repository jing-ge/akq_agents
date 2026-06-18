"""P3 Factor 协议 + FactorRegistry。

每个 factor 是一个声明性对象，实现 :meth:`compute(ohlcv) -> pd.Series`。
``factor_version`` 字段必须 >= 1，改算法时 +1；用于 `factor_metrics` 表的版本绑定
（P3 附录 B §2 承诺）。

P3a：``list_active`` 直接返回 ``list_all``，不读 metrics 做失能判定。
P3b：升级为读 ``factor_metrics`` 最近 ``status='active'`` 子集。

注：``Factor`` 用 ``Protocol`` 做结构化类型，**没有用 ``runtime_checkable``**——
我们依赖 duck-typing；任何实现了 ``name`` / ``factor_version`` / ``lookback_days`` /
``direction`` / ``inputs`` / ``compute`` 的对象都可视为 Factor。
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

import pandas as pd

FactorDirection = Literal["long", "short"]
FactorInput = Literal["ohlcv", "industry", "financials"]


class Factor(Protocol):
    """声明性 Factor 协议。结构化类型，不做 runtime isinstance 检查。"""

    name: str
    factor_version: int
    inputs: tuple[str, ...]
    lookback_days: int
    direction: str

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        """计算因子原始值。

        Args:
            ohlcv: long-format DataFrame，列 ``[date, symbol, open, high, low, close, volume, amount]``，
                包含 max(lookback_days) 个交易日的数据。

        Returns:
            ``index=symbol, values=raw_factor_value`` 的 Series。允许 NaN（缺数据）。
        """
        ...


class FactorRegistry:
    """全局因子注册表。

    注册时强校验 ``name`` 唯一 + ``factor_version >= 1``。
    """

    def __init__(self, evaluator: object | None = None) -> None:
        self._factors: dict[str, Factor] = {}
        self._evaluator = evaluator

    def attach_evaluator(self, evaluator: object) -> None:
        """供 bootstrap 注入 evaluator 用于 list_active 失能判定。"""
        self._evaluator = evaluator

    def register(self, factor: Factor) -> None:
        if not getattr(factor, "name", None):
            raise ValueError(f"factor must have non-empty name: {factor!r}")
        if factor.factor_version < 1:
            raise ValueError(f"factor.factor_version must be >= 1, got {factor.factor_version!r}")
        if factor.name in self._factors:
            existing = self._factors[factor.name]
            if existing.factor_version == factor.factor_version:
                raise ValueError(
                    f"factor {factor.name!r} v{factor.factor_version} already registered"
                )
        self._factors[factor.name] = factor

    def get(self, name: str) -> Factor:
        if name not in self._factors:
            raise KeyError(f"factor {name!r} not registered")
        return self._factors[name]

    def list_all(self) -> list[Factor]:
        return list(self._factors.values())

    def list_active(self, as_of_date: date) -> list[Factor]:
        """按最近一次 metric.status 过滤；inactive 的因子不参与组合合成。

        没有 evaluator 或没有 metric 时退化为 list_all（避免新因子被永远屏蔽）。
        """
        _ = as_of_date
        if self._evaluator is None:
            return self.list_all()
        active: list[Factor] = []
        for f in self._factors.values():
            try:
                m = self._evaluator.get_latest(f.name, f.factor_version)  # type: ignore[attr-defined]
            except Exception:
                m = None
            if m is None or getattr(m, "status", "active") != "inactive":
                active.append(f)
        return active

    def factor_directions(self) -> dict[str, str]:
        """快速查每个因子的 direction（用于 Preprocessor 反号）。"""
        return {f.name: f.direction for f in self._factors.values()}
