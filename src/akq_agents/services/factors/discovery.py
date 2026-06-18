"""自动因子发现引擎。

核心思想：
- **DSL 空间**：通过 base × op × window 三轴组合，可生成大量"算子-基线-窗口"候选因子；
- **运行时编译**：每个 recipe 在 `compute(ohlcv)` 时直接计算，无需手写 Factor 子类；
- **门槛筛选**：调用现有 `FactorEvaluator` 算 IC/IR，叠加"与已 active 因子的相关性"门槛；
- **持久化决策**：写入 `factor_proposals` 表，accepted 因子注册进 `FactorRegistry`。

设计原则（YAGNI）：
- 不写 LLM 生成因子；仅用结构化 DSL 即可产出几百种候选；
- 不引入符号回归/遗传算法；纯随机/穷举抽样足以提供发现能力；
- 一个候选的 compute 不需要是"最优"的——只要 IC/IR 满足门槛就接收。
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from akq_agents.services.factors.base import Factor, FactorRegistry
from akq_agents.services.factors.proposal_store import (
    FactorProposal,
    FactorProposalStore,
    now_iso,
    recipe_to_json,
)

logger = logging.getLogger(__name__)


# ----------------- DSL & 运行时 Factor ---------------------------------------

# base 列：对应 long-format ohlcv 的列名 / 衍生表达式
_BASES = {
    "close": lambda df: df["close"].astype(float),
    "volume": lambda df: df["volume"].astype(float),
    "amount": lambda df: df["amount"].astype(float) if "amount" in df.columns else df["close"] * df["volume"],
    "high_low_range": lambda df: (df["high"].astype(float) - df["low"].astype(float)),
    "vwap": lambda df: (df["amount"] / df["volume"].replace(0, np.nan)).astype(float)
    if "amount" in df.columns
    else df["close"].astype(float),
}

_OPS = ("pct_change", "rolling_mean", "rolling_std", "zscore", "rsi", "rolling_skew", "ts_max_norm", "ts_min_norm")

_WINDOWS = (5, 10, 20, 30, 60)

_DIRECTIONS = ("long", "short")


@dataclass
class _RuntimeFactor:
    """动态生成的 Factor 实现：实现 Factor 协议所需的全部字段 + compute。

    注意：duck-typed（FactorRegistry 不做 isinstance 检查）。
    """

    name: str
    factor_version: int
    lookback_days: int
    direction: str
    base: str
    op: str
    window: int
    inputs: tuple[str, ...] = ("ohlcv",)

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        if ohlcv.empty:
            return pd.Series(dtype=float, name=self.name)
        # 以 symbol 维度透视成 wide table（index=date, columns=symbol）
        base_long = _BASES[self.base](ohlcv).rename("v")
        wide = (
            pd.DataFrame({"date": ohlcv["date"], "symbol": ohlcv["symbol"], "v": base_long})
            .pivot_table(index="date", columns="symbol", values="v", aggfunc="last")
            .sort_index()
        )
        out = _apply_op(wide, self.op, self.window)
        if out is None:
            return pd.Series({sym: np.nan for sym in wide.columns}, name=self.name)
        last = out.iloc[-1]
        last.name = self.name
        return last.replace([np.inf, -np.inf], np.nan)


def _apply_op(wide: pd.DataFrame, op: str, window: int) -> pd.DataFrame | None:
    if len(wide) < window + 1:
        return None
    if op == "pct_change":
        return wide.pct_change(periods=window, fill_method=None)
    if op == "rolling_mean":
        return wide.rolling(window).mean()
    if op == "rolling_std":
        return wide.pct_change(fill_method=None).rolling(window).std()
    if op == "zscore":
        rolled = wide.rolling(window)
        return (wide - rolled.mean()) / rolled.std(ddof=0).replace(0, np.nan)
    if op == "rsi":
        delta = wide.diff()
        gain = delta.clip(lower=0).rolling(window).mean()
        loss = (-delta.clip(upper=0)).rolling(window).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))
    if op == "rolling_skew":
        return wide.pct_change(fill_method=None).rolling(window).skew()
    if op == "ts_max_norm":
        return wide / wide.rolling(window).max().replace(0, np.nan) - 1.0
    if op == "ts_min_norm":
        return wide / wide.rolling(window).min().replace(0, np.nan) - 1.0
    raise ValueError(f"unknown op: {op}")


def _recipe_dict(base: str, op: str, window: int, direction: str) -> dict:
    return {"base": base, "op": op, "window": window, "direction": direction}


def _recipe_to_name(recipe: dict) -> str:
    """生成稳定且可读的因子名。"""
    h = hashlib.md5(recipe_to_json(recipe).encode("utf-8")).hexdigest()[:6]
    return f"auto_{recipe['op']}_{recipe['base']}_{recipe['window']}_{recipe['direction']}_{h}"


def make_factor(recipe: dict, *, factor_version: int = 1) -> Factor:
    """从 recipe 字典实例化一个 Runtime Factor（duck-typed Factor）。"""
    name = _recipe_to_name(recipe)
    lookback = max(recipe["window"] * 3, 60)  # 给评估留余量
    f = _RuntimeFactor(
        name=name,
        factor_version=factor_version,
        lookback_days=lookback,
        direction=recipe["direction"],
        base=recipe["base"],
        op=recipe["op"],
        window=recipe["window"],
    )
    return f  # type: ignore[return-value]


# ----------------- 因子空间生成器 -------------------------------------------


@dataclass
class FactorSpace:
    """三轴笛卡尔积空间 + 随机/穷举抽样。"""

    bases: tuple[str, ...] = tuple(_BASES.keys())
    ops: tuple[str, ...] = _OPS
    windows: tuple[int, ...] = _WINDOWS
    directions: tuple[str, ...] = _DIRECTIONS

    def size(self) -> int:
        return len(self.bases) * len(self.ops) * len(self.windows) * len(self.directions)

    def sample(self, n: int, rng: random.Random | None = None) -> list[dict]:
        rng = rng or random.Random()
        seen: set[str] = set()
        out: list[dict] = []
        while len(out) < n and len(seen) < self.size():
            r = _recipe_dict(
                base=rng.choice(self.bases),
                op=rng.choice(self.ops),
                window=rng.choice(self.windows),
                direction=rng.choice(self.directions),
            )
            key = recipe_to_json(r)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out


# ----------------- 发现引擎 -------------------------------------------------


@dataclass
class DiscoveryThresholds:
    min_abs_ic: float = 0.015
    min_ir: float = 0.25
    max_abs_corr: float = 0.7  # 与已 accepted 因子的相关性上限
    min_window_days: int = 30  # 评估窗口下限


@dataclass
class DiscoveryStats:
    proposed: int = 0
    accepted: int = 0
    rejected_low_ic: int = 0
    rejected_low_ir: int = 0
    rejected_high_corr: int = 0
    rejected_compute_error: int = 0
    rejected_insufficient_data: int = 0
    duplicates_skipped: int = 0
    accepted_names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "accepted": self.accepted,
            "rejected_low_ic": self.rejected_low_ic,
            "rejected_low_ir": self.rejected_low_ir,
            "rejected_high_corr": self.rejected_high_corr,
            "rejected_compute_error": self.rejected_compute_error,
            "rejected_insufficient_data": self.rejected_insufficient_data,
            "duplicates_skipped": self.duplicates_skipped,
            "accepted_names": self.accepted_names,
        }


class DiscoveryEngine:
    """从因子空间抽样候选 → 评估 → 通过门槛 → 注册到 registry + 持久化。

    依赖（构造时注入）:
        repository: P1 DataRepository，用于读 OHLCV
        registry: 现有内存 FactorRegistry（accepted 因子会 register 进去）
        evaluator: FactorEvaluator（也会写 factor_metrics，无需我们另外算 IC）
        proposal_store: FactorProposalStore
    """

    def __init__(
        self,
        *,
        repository: Any,
        registry: FactorRegistry,
        evaluator: Any,
        proposal_store: FactorProposalStore,
        space: FactorSpace | None = None,
        thresholds: DiscoveryThresholds | None = None,
        random_seed: int | None = None,
    ) -> None:
        self.repo = repository
        self.registry = registry
        self.evaluator = evaluator
        self.proposal_store = proposal_store
        self.space = space or FactorSpace()
        self.th = thresholds or DiscoveryThresholds()
        self._rng = random.Random(random_seed)

    # ------------------------------------------------------------------

    def run_batch(self, *, n_candidates: int, as_of_date: date | None = None) -> DiscoveryStats:
        """跑一轮发现。返回统计。"""
        as_of_date = as_of_date or date.today()
        stats = DiscoveryStats()

        # 1) 准备数据：top 500 流动性 universe + 区间 OHLCV
        try:
            ohlcv, sub_symbols = self._prepare_data(as_of_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discovery: prepare_data failed: %s", exc)
            return stats
        if ohlcv.empty:
            return stats

        # 2) 已 active 因子的当日值矩阵（用于相关性筛选）
        active_factor_history = self._compute_active_factor_history(ohlcv)

        # 3) close 旋转 + forward returns（用于 IC 计算）
        close = ohlcv.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        forward_returns = close.pct_change(fill_method=None).shift(-1)

        # 4) 抽样候选
        candidates = self.space.sample(n_candidates, rng=self._rng)
        stats.proposed = len(candidates)
        evaluated_at = now_iso()

        for recipe in candidates:
            name = _recipe_to_name(recipe)
            if self.proposal_store.exists(name):
                stats.duplicates_skipped += 1
                continue

            factor = make_factor(recipe)

            # 计算 factor history（每个 as_of_date 用截止那日的数据）
            try:
                factor_history = self._compute_factor_history(factor, ohlcv, close.index)
            except Exception as exc:  # noqa: BLE001
                logger.debug("discovery: compute_history failed for %s: %s", name, exc)
                self._record(name, recipe, "rejected", reason=f"compute_error: {exc}",
                             ic_mean=None, ic_std=None, ir=None, t_stat=None,
                             max_abs_corr=None, evaluated_at=evaluated_at)
                stats.rejected_compute_error += 1
                continue

            if factor_history is None or len(factor_history.dropna(how="all")) < self.th.min_window_days:
                self._record(name, recipe, "rejected", reason="insufficient_data",
                             ic_mean=None, ic_std=None, ir=None, t_stat=None,
                             max_abs_corr=None, evaluated_at=evaluated_at)
                stats.rejected_insufficient_data += 1
                continue

            # 直接复用 FactorEvaluator 的逻辑算 IC/IR（同时写入 factor_metrics 表）
            metric = self.evaluator.evaluate(
                factor=factor,
                factor_history=factor_history,
                forward_returns=forward_returns,
                as_of_date=as_of_date,
            )

            ic_mean = metric.ic_mean or 0.0
            ir = metric.ir or 0.0
            t_stat = metric.t_stat

            if abs(ic_mean) < self.th.min_abs_ic:
                self._record(name, recipe, "rejected", reason="low_ic",
                             ic_mean=ic_mean, ic_std=metric.ic_std,
                             ir=ir, t_stat=t_stat, max_abs_corr=None,
                             evaluated_at=evaluated_at)
                stats.rejected_low_ic += 1
                continue
            if abs(ir) < self.th.min_ir:
                self._record(name, recipe, "rejected", reason="low_ir",
                             ic_mean=ic_mean, ic_std=metric.ic_std,
                             ir=ir, t_stat=t_stat, max_abs_corr=None,
                             evaluated_at=evaluated_at)
                stats.rejected_low_ir += 1
                continue

            # 相关性筛选：与已 active 因子取最大绝对 Spearman 相关
            max_abs_corr = self._max_abs_corr(factor_history, active_factor_history)
            if max_abs_corr is not None and max_abs_corr > self.th.max_abs_corr:
                self._record(name, recipe, "rejected", reason="high_corr",
                             ic_mean=ic_mean, ic_std=metric.ic_std,
                             ir=ir, t_stat=t_stat, max_abs_corr=max_abs_corr,
                             evaluated_at=evaluated_at)
                stats.rejected_high_corr += 1
                continue

            # 通过 → 注册 + 持久化
            try:
                self.registry.register(factor)
            except ValueError:
                # 名字撞了（理论上不应该；hash 已唯一）→ 当 duplicate
                stats.duplicates_skipped += 1
                continue
            self._record(name, recipe, "accepted", reason="ok",
                         ic_mean=ic_mean, ic_std=metric.ic_std,
                         ir=ir, t_stat=t_stat, max_abs_corr=max_abs_corr,
                         evaluated_at=evaluated_at)
            stats.accepted += 1
            stats.accepted_names.append(name)

            # 把新接收的因子也加入 active_factor_history，影响后续候选的相关性筛选
            active_factor_history[name] = factor_history.iloc[-1] if len(factor_history) else pd.Series()

        return stats

    # ------------------------------------------------------------------

    def _prepare_data(self, as_of_date: date) -> tuple[pd.DataFrame, list[str]]:
        from datetime import timedelta

        full = self.repo.get_universe(as_of_date)
        # 用 PortfolioAgent 同款的 loose read 避免 DataNotReady
        max_lookback = 180
        start = as_of_date - timedelta(days=max_lookback * 2)
        ohlcv = self._loose_read_ohlcv(full.symbols, start, as_of_date)
        if ohlcv.empty:
            return ohlcv, []
        from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

        sub_symbols = build_portfolio_universe(
            full_universe_symbols=full.symbols, ohlcv=ohlcv, top_n=300, window=20
        )
        sub = ohlcv[ohlcv["symbol"].isin(list(sub_symbols))]
        return sub.reset_index(drop=True), list(sub_symbols)

    def _loose_read_ohlcv(self, symbols, start: date, end: date) -> pd.DataFrame:
        import pyarrow.dataset as ds

        ohlcv_root = getattr(self.repo, "_ohlcv_dir", None)
        if ohlcv_root is None or not ohlcv_root.exists():
            return pd.DataFrame()
        dataset = ds.dataset(ohlcv_root, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(list(symbols)),
        )
        frame = table.to_pandas()
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    def _compute_factor_history(
        self, factor: Factor, ohlcv: pd.DataFrame, all_dates: pd.Index
    ) -> pd.DataFrame:
        """对每个 as_of_date 用截止那日的 ohlcv 计算 factor 横截面值。"""
        rows: dict[Any, pd.Series] = {}
        # 速度优化：只在每 5 个交易日取一个采样点（足以算 60 天 IR），减少 O(N) 复杂度
        # 注意：FactorEvaluator 内部仍按日做 Spearman，所以稀疏采样会让其 ic 序列变短，
        # 这里我们干脆把窗口缩短到稀疏点数量。
        sampled = list(all_dates)[::3]  # 每 3 个交易日采一次
        for d in sampled:
            d_date = d.date() if hasattr(d, "date") else d
            sub = ohlcv[ohlcv["date"] <= d_date]
            if len(sub) < factor.lookback_days:
                continue
            try:
                s = factor.compute(sub)
            except Exception:
                continue
            if s is None or s.empty:
                continue
            rows[d] = s
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).T

    def _compute_active_factor_history(self, ohlcv: pd.DataFrame) -> dict[str, pd.Series]:
        """取最后一日已 active 因子的横截面值，用于做相关性筛选。"""
        out: dict[str, pd.Series] = {}
        for f in self.registry.list_all():
            try:
                s = f.compute(ohlcv)
                if s is not None and not s.empty:
                    out[f.name] = s
            except Exception:
                continue
        return out

    @staticmethod
    def _max_abs_corr(
        factor_history: pd.DataFrame, others: dict[str, pd.Series]
    ) -> float | None:
        """新因子最后一日横截面 vs 每个已 active 因子横截面的 Spearman 相关，取 max abs。"""
        if factor_history.empty or not others:
            return None
        s_new = factor_history.iloc[-1].dropna()
        if len(s_new) < 5:
            return None
        max_corr = 0.0
        for name, s_other in others.items():
            common = s_new.index.intersection(s_other.dropna().index)
            if len(common) < 5:
                continue
            try:
                c = s_new.loc[common].rank().corr(s_other.loc[common].rank())
            except Exception:
                continue
            if c is not None and not pd.isna(c):
                max_corr = max(max_corr, abs(float(c)))
        return max_corr if max_corr > 0 else None

    def _record(
        self,
        name: str,
        recipe: dict,
        status: str,
        *,
        reason: str | None,
        ic_mean: float | None,
        ic_std: float | None,
        ir: float | None,
        t_stat: float | None,
        max_abs_corr: float | None,
        evaluated_at: str | None,
    ) -> None:
        proposal = FactorProposal(
            factor_name=name,
            recipe_json=recipe_to_json(recipe),
            direction=recipe["direction"],
            status=status,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=ir,
            t_stat=t_stat,
            max_abs_corr=max_abs_corr,
            reason=reason,
            created_at=now_iso(),
            evaluated_at=evaluated_at,
        )
        self.proposal_store.upsert(proposal)


def restore_accepted_factors(
    registry: FactorRegistry, proposal_store: FactorProposalStore
) -> int:
    """启动期：把数据库里 accepted 的因子重新 register 到内存 registry。"""
    from akq_agents.services.factors.proposal_store import recipe_from_json

    count = 0
    for p in proposal_store.list_accepted():
        try:
            recipe = recipe_from_json(p.recipe_json)
            factor = make_factor(recipe)
            registry.register(factor)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("restore factor %s failed: %s", p.factor_name, exc)
    return count
