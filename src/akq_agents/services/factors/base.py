"""P3 Factor 协议 + FactorRegistry + CodeFactor (重构新增)。

每个 factor 是一个声明性对象，实现 :meth:`compute(ohlcv) -> pd.Series`。
``factor_version`` 字段必须 >= 1，改算法时 +1；用于 `factor_metrics` 表的版本绑定
（P3 附录 B §2 承诺）。

P3a：``list_active`` 直接返回 ``list_all``，不读 metrics 做失能判定。
P3b：升级为读 ``factor_metrics`` 最近 ``status='active'`` 子集。

注：``Factor`` 用 ``Protocol`` 做结构化类型，**没有用 ``runtime_checkable``**——
我们依赖 duck-typing；任何实现了 ``name`` / ``factor_version`` / ``lookback_days`` /
``direction`` / ``inputs`` / ``compute`` 的对象都可视为 Factor。

重构新增：``CodeFactor`` 是一个持有 source code 的因子实例，compute 行为
由 sandbox 编译过的 callable 提供。FactorRegistry.register 也接受它。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol, runtime_checkable

import pandas as pd

FactorDirection = Literal["long", "short"]
FactorInput = Literal["ohlcv", "industry", "financials"]


def compute_forward_returns(close: pd.DataFrame) -> pd.DataFrame:
    """T+1 前瞻收益的**唯一规范定义** (前视偏差防护关键点)。

    ``forward_returns.loc[d]`` = 第 d 日 → 第 d+1 日的收益, 即
    ``close.pct_change().shift(-1)``。用于 IC 计算时与"截止 d 日的因子值"配对,
    保证「用 T 日信息预测 T→T+1 收益」, 不引入未来数据。

    **务必全项目统一走此 helper**: 历史上 shift(-1) 散落在 discovery /
    history_backfill / batch_deep_research / ic_diagnostics 等 4+ 处, 任一处
    漏写/错写 shift 都会静默引入前视偏差, 而 canary 只测配对算法测不到。
    收敛到单点后, 对本函数做 canary 即守住全部生产路径。

    ``fill_method=None``: 不对停牌缺口做前向填充, 避免用停牌前价格虚构收益。
    """
    return close.pct_change(fill_method=None).shift(-1)



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


# ----------------------------------------------------------------------------
# 重构: CodeFactor — LLM 自由 Python 代码作为 compute 行为
# ----------------------------------------------------------------------------


@dataclass
class CodeFactor:
    """LLM 输出的 Python 因子（不限定 base/op/window 笛卡尔积）。

    特点:
    - ``source_code`` 是 LLM 写的 Python 源码, 约定定义 ``def compute(ohlcv) -> pd.Series``
    - ``_fn`` 是 sandbox 编译后的可调用对象, 真正执行 compute
    - duck-typed Factor: registry.register 只看 name/version/lookback_days/direction/inputs
    - 默认 ``lookback_days=60`` (LLM 不指定时给个保守值, evaluation 时再调)

    安全: source_code 必须先过 :mod:`sandbox` 的 AST 静态检查才能编译.
    """

    name: str
    source_code: str
    fn: Callable[[pd.DataFrame], pd.Series]
    factor_version: int = 1
    direction: str = "long"
    inputs: tuple[str, ...] = ("ohlcv",)
    lookback_days: int = 60
    # 元信息 (LLM 自填, 用于审核/可读性)
    description: str = ""
    code_hash: str = ""  # sha1(source_code) 跨 session 去重

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        # sandbox 编译出来的 fn 已经做了错误处理; 这里再包一层防御
        if ohlcv is None or ohlcv.empty:
            return pd.Series(dtype=float, name=self.name)
        try:
            out = self.fn(ohlcv)
        except Exception:
            # compute 失败 → 全 NaN, 不让异常污染上层 (DiscoveryEngine 会拿 IC=NaN 算 rejected)
            return pd.Series({sym: float("nan") for sym in ohlcv["symbol"].unique()},
                             name=self.name)
        if not isinstance(out, pd.Series):
            # 兜底: LLM 可能写错返回 dict / np.ndarray → 强转
            try:
                out = pd.Series(out)
            except Exception:
                return pd.Series(dtype=float, name=self.name)
        # 强制覆盖 name → 始终用 self.name, 避免 LLM 的 compute() 拿 wide.iloc[-1] 时
        # 把 date 当 name, 下游 factor_metrics / portfolio_attribution 用 name 做 key 会错位
        out = out.rename(self.name)
        return out.replace([float("inf"), float("-inf")], float("nan"))


# runtime_checkable 让 isinstance 风格的 code_factor 检查能 work (debug/日志用)
@runtime_checkable
class _HasCode(Protocol):
    source_code: str
    fn: Callable[[pd.DataFrame], pd.Series]
