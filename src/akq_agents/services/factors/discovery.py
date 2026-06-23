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
    min_ir: float = 0.30          # M7-C: 提高到 0.30（in-sample 偏乐观）
    max_abs_corr: float = 0.7
    min_window_days: int = 60     # M7-C: 至少 60 个交易日才认 IC 估计
    # M7-C: OOS promote 规则
    shadow_min_oos_days: int = 20      # 至少累计 20 个 OOS 交易日观察
    shadow_min_oos_ir: float = 0.15    # OOS IR 仍需 >= 0.15 才 promote


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
        state_store: Any = None,
    ) -> None:
        self.repo = repository
        self.registry = registry
        self.evaluator = evaluator
        self.proposal_store = proposal_store
        self.space = space or FactorSpace()
        self.th = thresholds or DiscoveryThresholds()
        self._rng = random.Random(random_seed)
        # I5: 可选 SchedulerStateStore，让 silent fallback 能写 events 到 /ops 看板
        self._state_store = state_store

    def _write_event_safe(self, kind: str, error_msg: str) -> None:
        """I5: silent fallback 路径补 events 记账。"""
        if self._state_store is None:
            return
        try:
            self._state_store.write_event(
                level="warning",
                kind=kind,
                source="discovery_engine",
                payload={"error": error_msg[:300]},
            )
        except Exception:  # noqa: BLE001
            pass

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

        # 3) close 旋转 + forward returns（用于 IC 计算）
        close = ohlcv.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        forward_returns = close.pct_change(fill_method=None).shift(-1)

        # 2) 已 active 因子的完整历史矩阵（用于时间序列相关性筛选）
        active_factor_history = self._compute_active_factor_history(ohlcv, close.index)

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

            # 通过 IS 门槛 → 进入 shadow 状态（**不立刻注册到内存 registry**）
            # 必须先通过 OOS 观察期才能 promote 到 active
            self._record_with_shadow(
                name, recipe,
                ic_mean=ic_mean, ic_std=metric.ic_std,
                ir=ir, t_stat=t_stat, max_abs_corr=max_abs_corr,
                evaluated_at=evaluated_at,
            )
            stats.accepted += 1  # 这里"accepted"语义保持向后兼容（计入"通过门槛"）
            stats.accepted_names.append(name)

            # 把新候选也加入 active_factor_history（影响后续 candidate 的相关性判定）
            active_factor_history[name] = factor_history

        # 4) 处理已存在的 shadow 因子：检查 OOS 是否满足 promote 条件
        # I5: silent fallback 整体包一层，失败时写 events（不影响主流程）
        try:
            self._promote_shadows(stats=stats, as_of_date=as_of_date)
        except Exception as exc:  # noqa: BLE001
            self._write_event_safe("factor.promote_shadows_failed", str(exc))

        # P1-4: DSL 空间耗尽告警 —— 如果 duplicates 占比超过 80%，提示扩 DSL
        if stats.proposed > 0:
            dup_ratio = stats.duplicates_skipped / stats.proposed
            if dup_ratio >= 0.8:
                logger.warning(
                    "factor.space_exhausted: duplicates %.0f%% (%d/%d) — DSL 空间快被穷举完了，"
                    "考虑扩 _OPS / _WINDOWS / _BASES",
                    dup_ratio * 100, stats.duplicates_skipped, stats.proposed,
                )

        return stats

    # ------------------------------------------------------------------

    def _prepare_data(self, as_of_date: date) -> tuple[pd.DataFrame, list[str]]:
        from datetime import timedelta

        full = self.repo.get_universe(as_of_date)
        # 用 PortfolioAgent 同款的 loose read 避免 DataNotReady
        max_lookback = 180
        start = as_of_date - timedelta(days=max_lookback * 2)
        ohlcv = self.repo.get_ohlcv_loose(full.symbols, start, as_of_date)
        if ohlcv.empty:
            return ohlcv, []
        from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

        sub_symbols = build_portfolio_universe(
            full_universe_symbols=full.symbols, ohlcv=ohlcv, top_n=300, window=20
        )
        sub = ohlcv[ohlcv["symbol"].isin(list(sub_symbols))]
        return sub.reset_index(drop=True), list(sub_symbols)

    def _compute_factor_history(
        self, factor: Factor, ohlcv: pd.DataFrame, all_dates: pd.Index
    ) -> pd.DataFrame:
        """对每个 as_of_date 用截止那日的 ohlcv 计算 factor 横截面值。

        M7-C: 改为 daily（每个交易日都算），window 单位与交易日一致；不再 [::3]
        稀疏采样。性能上 daily 比稀疏 3x 慢，但准确性显著提升。
        如果实际性能成问题，未来可以改成"先 wide compute（pivot），再 rolling"，
        但这要把每个 op 改写成可向量化版本，YAGNI。
        """
        rows: dict[Any, pd.Series] = {}
        # 至少要有 lookback_days 数据才能开始评估
        for d in all_dates:
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

    def _compute_active_factor_history(self, ohlcv: pd.DataFrame, all_dates: pd.Index) -> dict[str, pd.DataFrame]:
        """计算所有已 active 因子的完整 history（用于时间序列相关性筛选）。

        返回：{factor_name: DataFrame(index=date, columns=symbol)}
        """
        out: dict[str, pd.DataFrame] = {}
        for f in self.registry.list_all():
            try:
                hist = self._compute_factor_history(f, ohlcv, all_dates)
                if hist is not None and not hist.empty:
                    out[f.name] = hist
            except Exception:
                continue
        return out

    @staticmethod
    def _max_abs_corr(
        factor_history: pd.DataFrame, others: dict[str, pd.DataFrame]
    ) -> float | None:
        """新因子 vs 每个已 active 因子的"时间序列相关性"：
        在每个日期 t 上把两个因子横截面 rank 化后做 Spearman，得到 IC-IC 时序，
        再取时序的均值 → 取所有 active 因子里绝对值最大的那个。

        这比"只看最后一日横截面"的判别更稳，能识别"形似但相位不同"的因子。
        """
        if factor_history.empty or not others:
            return None
        if len(factor_history) < 5:
            return None
        max_corr = 0.0
        for name, hist in others.items():
            if hist.empty:
                continue
            # 对齐日期
            common_dates = factor_history.index.intersection(hist.index)
            if len(common_dates) < 5:
                continue
            # 每个日期算横截面 Spearman，然后取平均
            corrs = []
            for d in common_dates:
                s_new = factor_history.loc[d].dropna()
                s_other = hist.loc[d].dropna()
                common_syms = s_new.index.intersection(s_other.index)
                if len(common_syms) < 5:
                    continue
                try:
                    c = s_new.loc[common_syms].rank().corr(s_other.loc[common_syms].rank())
                except Exception:
                    continue
                if c is not None and not pd.isna(c):
                    corrs.append(float(c))
            if corrs:
                avg_corr = float(np.mean(corrs))
                max_corr = max(max_corr, abs(avg_corr))
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

    def _record_with_shadow(
        self,
        name: str,
        recipe: dict,
        *,
        ic_mean: float | None,
        ic_std: float | None,
        ir: float | None,
        t_stat: float | None,
        max_abs_corr: float | None,
        evaluated_at: str | None,
    ) -> None:
        """通过 IS 门槛的因子写入 status='shadow' + shadow_started_at=now。

        注意：不调用 registry.register —— shadow 因子不参与组合合成，只在 OOS 期接受观察。
        """
        ts = now_iso()
        proposal = FactorProposal(
            factor_name=name,
            recipe_json=recipe_to_json(recipe),
            direction=recipe["direction"],
            status="shadow",
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=ir,
            t_stat=t_stat,
            max_abs_corr=max_abs_corr,
            reason="passed_is_pending_oos",
            created_at=ts,
            evaluated_at=evaluated_at,
            shadow_started_at=ts,
            oos_observations=0,
            oos_ir=None,
        )
        self.proposal_store.upsert(proposal)

    def _promote_shadows(self, *, stats: "DiscoveryStats", as_of_date: date) -> None:
        """遍历 shadow 因子，根据 shadow_started_at 算出累计 OOS 天数。

        - 累计 OOS 天数 < shadow_min_oos_days：跳过
        - 达到时长：重新算 OOS 期间（自 shadow_started_at 后）的 IR
          - 通过 shadow_min_oos_ir：promote → 'accepted' + register 到内存 registry
          - 否则：demote → 'demoted'（不会再被复评，避免无限重试）
        """
        from datetime import datetime as _dt

        shadow_list = self.proposal_store.list_shadow()
        if not shadow_list:
            return

        # 准备共享数据（一次性拉）
        try:
            ohlcv, _ = self._prepare_data(as_of_date)
        except Exception as exc:  # noqa: BLE001
            self._write_event_safe("factor.discovery.prepare_data_failed", str(exc))
            return
        if ohlcv.empty:
            return
        close = ohlcv.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        forward_returns = close.pct_change(fill_method=None).shift(-1)
        all_dates = close.index

        for p in shadow_list:
            if p.shadow_started_at is None:
                continue
            try:
                shadow_dt = _dt.fromisoformat(p.shadow_started_at)
            except Exception:
                continue
            # 把 shadow 开始时间映射到交易日
            shadow_d = shadow_dt.date()
            # OOS 期 = [shadow_d 之后的交易日]
            oos_dates = [d for d in all_dates if (d.date() if hasattr(d, "date") else d) > shadow_d]
            if len(oos_dates) < self.th.shadow_min_oos_days:
                # 更新 oos_observations 计数，但不 promote
                p.oos_observations = len(oos_dates)
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue

            # 满足时长 → 重新算 OOS IR
            from akq_agents.services.factors.proposal_store import recipe_from_json
            try:
                recipe = recipe_from_json(p.recipe_json)
                factor = make_factor(recipe)
            except Exception:
                continue
            try:
                hist = self._compute_factor_history(factor, ohlcv, all_dates)
            except Exception:
                continue
            if hist.empty:
                continue
            # 只看 OOS 期间
            oos_hist = hist.loc[hist.index.isin(oos_dates)]
            oos_ret = forward_returns.loc[forward_returns.index.isin(oos_dates)]
            if len(oos_hist) < 5:
                continue
            # 复用 evaluator._rolling_ic 算 IR
            from akq_agents.services.portfolio.evaluator import _rolling_ic
            ic_series = _rolling_ic(oos_hist, oos_ret, window=min(len(oos_hist), 60))
            ic_clean = ic_series.dropna()
            if len(ic_clean) < 5:
                continue
            oos_ic_mean = float(ic_clean.mean())
            oos_ic_std = float(ic_clean.std(ddof=1)) if ic_clean.std(ddof=1) > 0 else None
            oos_ir = (oos_ic_mean / oos_ic_std) if oos_ic_std else None

            if oos_ir is not None and abs(oos_ir) >= self.th.shadow_min_oos_ir:
                # Promote → accepted + register
                # M9-B: 如果 OOS IR 为负，说明原 direction 反了，自动反转
                if oos_ir < 0:
                    new_direction = "short" if recipe["direction"] == "long" else "long"
                    flipped_recipe = dict(recipe)
                    flipped_recipe["direction"] = new_direction
                    # 注意：name 是 hash 包含 direction 的，反转后 name 也变。
                    # 但我们不希望生成新条目（会失去 OOS 历史），所以保留原 factor_name，
                    # 只更新 recipe_json + direction，使 make_factor 用反转后的版本。
                    factor = make_factor(flipped_recipe)
                    # 强制把 factor.name 改回原 name（保持 db 主键）
                    factor.name = p.factor_name  # type: ignore[attr-defined]
                    p.recipe_json = recipe_to_json(flipped_recipe)
                    p.direction = new_direction
                    effective_ir = -oos_ir  # 反向后等价于正 IR
                    flip_note = f", direction_flipped (was {recipe['direction']})"
                else:
                    effective_ir = oos_ir
                    flip_note = ""
                # 保持 registry 里的 factor.name 与 db 主键 factor_name 一致
                # （否则 LLM 提议的 llm_* 因子在 promote 时会以 auto_* 注册到 registry，
                # 与 proposal_store 中的 llm_* 分裂，下游 factor_metrics 历史断裂）
                factor.name = p.factor_name  # type: ignore[attr-defined]
                try:
                    self.registry.register(factor)
                except ValueError:
                    pass
                p.status = "accepted"
                p.reason = f"promoted_after_{len(oos_dates)}d_oos_ir={oos_ir:.3f}{flip_note}"
                p.ir = effective_ir  # 把 IR 也更新成"有效方向后"的正值
                p.oos_observations = len(oos_dates)
                p.oos_ir = effective_ir
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                stats.accepted_names.append(p.factor_name + " (promoted)")
                logger.info(
                    "discovery: shadow %s PROMOTED (oos_ir=%.3f over %d days%s)",
                    p.factor_name, oos_ir, len(oos_dates), flip_note,
                )
            else:
                p.status = "demoted"
                p.reason = f"oos_ir_too_low_{oos_ir:.3f}" if oos_ir is not None else "oos_ir_undefined"
                p.oos_observations = len(oos_dates)
                p.oos_ir = oos_ir
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                logger.info(
                    "discovery: shadow %s DEMOTED (oos_ir=%s over %d days)",
                    p.factor_name, oos_ir, len(oos_dates),
                )


def restore_accepted_factors(
    registry: FactorRegistry, proposal_store: FactorProposalStore
) -> int:
    """启动期：把数据库里 status='accepted' 的因子重新 register 到内存 registry。

    注意 list_accepted() 现在同时返回 accepted + shadow，但 shadow 不参与组合 →
    我们这里只 register 真 accepted。
    """
    from akq_agents.services.factors.proposal_store import recipe_from_json

    count = 0
    for p in proposal_store.list_accepted():
        if p.status != "accepted":
            continue  # shadow 不进 registry
        try:
            recipe = recipe_from_json(p.recipe_json)
            factor = make_factor(recipe)
            # 强制保持 db 里的 factor_name（即便 recipe 改过 direction）
            # 这样 factor_metrics / portfolio_attribution 等历史表的 key 一致
            factor.name = p.factor_name  # type: ignore[attr-defined]
            registry.register(factor)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("restore factor %s failed: %s", p.factor_name, exc)
    return count
