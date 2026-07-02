"""自动因子发现引擎。

核心思想（重构后, 双通道并存）:
- **DSL 空间 (auto 路径)**: 通过 base × op × window × direction 笛卡尔积, 结构化生成
  大量"算子-基线-窗口"候选因子; 安全稳定, 不依赖 LLM.
- **Code 空间 (LLM 路径)**: LLM 自由写 Python `compute(ohlcv) -> pd.Series`,
  走 sandbox 受限执行; 探索空间不受 DSL 约束限制.

- **运行时编译 (DSL)**: 每个 recipe 在 `compute(ohlcv)` 时直接计算, 无需手写 Factor 子类;
- **沙箱执行 (Code)**: source_code 走 sandbox.py AST 静态检查 + 受控 builtin exec,
  失败 / 危险 / 超时直接拒绝.
- **门槛筛选**: 两条路径共用 FactorEvaluator 算 IC/IR, 叠加"与已 active 因子的相关性"门槛;
- **持久化决策**: 写入 `factor_proposals` 表 (recipe_kind 区分 dsl/code),
  accepted 因子注册进 `FactorRegistry`.

设计原则（YAGNI）:
- DSL 路径不引入 LLM 生成; 5×8×5×2=400 组合, 抽样 + dedup 即可;
- Code 路径不引入符号回归/遗传算法; LLM 直出 Python 是最直接的"自由空间";
- 一个候选的 compute 不需要是"最优"的——只要 IC/IR 满足门槛就接收.
"""

from __future__ import annotations

import hashlib
import logging
import random
import warnings
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from akq_agents.services.factors.base import CodeFactor, Factor, FactorRegistry
from akq_agents.services.factors.proposal_store import (
    FactorProposal,
    FactorProposalStore,
    now_iso,
    recipe_to_json,
)
from akq_agents.services.factors.sandbox import (
    UnsafeCodeError,
    compile_code_factor,
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
    # pyright 对 pandas rolling/clip/replace 返回类型推断为 DataFrame|Series|ndarray 联合，
    # 但实际所有分支都返回 wide-format DataFrame。逐行 ignore 让类型检查闭嘴。
    if len(wide) < window + 1:
        return None
    # rolling().skew()/std() 在窗口起始段全 NaN 时会发 RuntimeWarning，是设计内行为，
    # 局部静音避免 daemon.log 噪音淹没真实告警。
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="All-NaN slice encountered", category=RuntimeWarning)
        if op == "pct_change":
            return wide.pct_change(periods=window, fill_method=None)  # pyright: ignore[reportReturnType]
        if op == "rolling_mean":
            return wide.rolling(window).mean()  # pyright: ignore[reportReturnType]
        if op == "rolling_std":
            return wide.pct_change(fill_method=None).rolling(window).std()  # pyright: ignore[reportReturnType, reportAttributeAccessIssue]
        if op == "zscore":
            rolled = wide.rolling(window)
            return (wide - rolled.mean()) / rolled.std(ddof=0).replace(0, np.nan)  # pyright: ignore[reportReturnType, reportAttributeAccessIssue]
        if op == "rsi":
            delta = wide.diff()
            gain = delta.clip(lower=0).rolling(window).mean()
            loss = (-delta.clip(upper=0)).rolling(window).mean()
            rs = gain / loss.replace(0, np.nan)  # pyright: ignore[reportAttributeAccessIssue]
            return 100 - (100 / (1 + rs))  # pyright: ignore[reportReturnType]
        if op == "rolling_skew":
            return wide.pct_change(fill_method=None).rolling(window).skew()  # pyright: ignore[reportReturnType, reportAttributeAccessIssue]
        if op == "ts_max_norm":
            return wide / wide.rolling(window).max().replace(0, np.nan) - 1.0  # pyright: ignore[reportReturnType, reportAttributeAccessIssue]
        if op == "ts_min_norm":
            return wide / wide.rolling(window).min().replace(0, np.nan) - 1.0
    raise ValueError(f"unknown op: {op}")


def _recipe_dict(base: str, op: str, window: int, direction: str) -> dict:
    return {"base": base, "op": op, "window": window, "direction": direction}


def _recipe_to_name(recipe: dict) -> str:
    """生成稳定且可读的因子名。"""
    h = hashlib.md5(recipe_to_json(recipe).encode("utf-8")).hexdigest()[:6]
    return f"auto_{recipe['op']}_{recipe['base']}_{recipe['window']}_{recipe['direction']}_{h}"


def make_factor(
    recipe: dict,
    *,
    factor_version: int = 1,
    name: str | None = None,
) -> Factor:
    """从 recipe 字典实例化一个 Runtime Factor (duck-typed Factor)。

    重构: 如果 recipe 里有 ``_source_code`` 字段, 走 sandbox 编译成 CodeFactor.
    这种情况一般只在 restore (启动期从 db 读回) 时发生 ——
    正在运行的 discovery 主流程只抽样 DSL recipe, LLM 提议走 LLMCodeBrainstormer.
    """
    if "_source_code" in recipe:
        # 启动期 restore 用: db 里的 recipe_json 里塞了 source_code (重 serialize)
        source = recipe["_source_code"]
        direction = recipe.get("direction", "long")
        try:
            fn, ch = compile_code_factor(source, timeout_s=10.0)
        except UnsafeCodeError:
            # restore 时 source 已变不安全 (人改库 / schema 漂移) → 退回 DSL 部分编译
            # 实际不应发生, 兜底
            raise
        return CodeFactor(
            name=name or f"code_unknown_{ch[:6]}",
            source_code=source,
            fn=fn,
            factor_version=factor_version,
            direction=direction,
            code_hash=ch,
            description=recipe.get("description", ""),
        )

    name = name or _recipe_to_name(recipe)
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
class CodeProposal:
    """Code-kind 候选 (LLM 出的 source_code, 不走 DSL).

    discovery 主流程目前不直接采样 (Code 来源主要是 LLM brainstormer),
    但保留数据结构以便 FactorSpace.sample_code_candidates 在未来从历史 rejected
    code 里 mutation 二次探索.
    """

    source_code: str
    code_hash: str
    direction: str = "long"
    description: str = ""


@dataclass
class FactorSpace:
    """重构: 三轴 DSL 笛卡尔积 + Code 子空间.

    DSL 路径: bases × ops × windows × directions (5×8×5×2=400)
    Code 路径: source_code 字符串, 不限制 base/op/window/direction, 由
    LLMCodeBrainstormer 在 brainstorming 时填充. discovery 自身不主动生成.
    """

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
    # M19 review (oracle): |IR|>=0.15 在 20 天样本下 t_stat ≈ 0.67, 统计上完全不显著.
    # 必须同时满足 |t_stat| >= 2.0 才 promote — t_stat 自然带样本量归一化,
    # 60 天 IR=0.15 跟 20 天 IR=0.15 的显著性差 1.7 倍, 不能等同对待.
    shadow_min_oos_t_stat: float = 2.0
    # M15-A: shadow 宽限期 — 满 shadow_min_oos_days 但未达标时不立刻 demote，
    # 继续观察到 shadow_max_days；满 shadow_max_days 仍 |IR| < shadow_min_keep_ir 才 demote。
    shadow_max_days: int = 60         # 最长观察 60 天
    shadow_min_keep_ir: float = 0.10  # 60 天后 |IR| < 0.10 就 demote


@dataclass
class DiscoveryStats:
    proposed: int = 0
    accepted: int = 0
    promoted: int = 0
    demoted: int = 0
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
            "promoted": self.promoted,
            "demoted": self.demoted,
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
            self._write_event_safe("factor.discovery.prepare_data_failed", str(exc))
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

            # M19: 进 shadow 之后立刻 backfill 90 天历史 IC 写 factor_metrics +
            # 同步 factor_proposals.ic/ir/t_stat. 用户审核界面立刻看到完整曲线 +
            # IS-IC 数据。复用本流程已经算好的 close/forward_returns 上下文,
            # 不重新拉数据 (~2.5s/因子, 主要是 90 次 evaluator.evaluate 写表)。
            try:
                from akq_agents.services.factors.history_backfill import (
                    HistoryBackfillContext,
                    backfill_one,
                )
                bf_ctx = HistoryBackfillContext.from_existing(
                    ohlcv=ohlcv,
                    close=close,
                    forward_returns=forward_returns,
                    window=getattr(self.evaluator, "_window", 60),
                    days=90, step=1,
                    as_of_date=as_of_date,
                )
                if bf_ctx is not None:
                    backfill_one(
                        factor, bf_ctx,
                        evaluator=self.evaluator,
                        proposal_store=self.proposal_store,
                        compute_factor_history=self._compute_factor_history,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("discovery: backfill_one(%s) failed: %s", name, exc)

        # M19: shadow OOS 评估 / promote / demote 已拆到独立 daily job `factor.promote_shadows`
        # (避免本流程 _prepare_data empty 时 shadow 计数无法推进).
        # _promote_shadows 方法本身保留, 由独立 job 直接调用。

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

        # M19 review: 周末 / 节假日 / 盘中 today 数据没刷 — 用 calendar 找最近交易日。
        # 旧逻辑 fallback today-1 跨周末会死 (周一 → 周日 → 还是没数据)。
        cal = getattr(self.repo, "_calendar", None)

        # M19-A: get_universe(today) 在 today 数据还没刷时会抛 DataNotReady,
        # fallback 用最近交易日 (历史滚动评估对 universe 精确性要求不高).
        try:
            full = self.repo.get_universe(as_of_date)
        except Exception as exc:  # noqa: BLE001
            fb = cal.previous_trading_day(as_of_date) if cal is not None else (as_of_date - timedelta(days=1))
            logger.warning("discovery._prepare_data: get_universe(%s) failed: %s; fallback to %s",
                           as_of_date, exc, fb)
            try:
                full = self.repo.get_universe(fb)
                as_of_date = fb  # 同步推进 as_of_date 让下面 get_ohlcv_loose 用同日期
            except Exception as exc2:  # noqa: BLE001
                logger.warning("discovery._prepare_data: universe fallback also failed: %s", exc2)
                self._write_event_safe(
                    "factor.discovery.universe_unavailable",
                    f"both as_of={as_of_date} and fallback={fb} failed: {exc2}",
                )
                return pd.DataFrame(), []

        # 用 PortfolioAgent 同款的 loose read 避免 DataNotReady
        max_lookback = 180
        start = as_of_date - timedelta(days=max_lookback * 2)
        ohlcv = self.repo.get_ohlcv_loose(full.symbols, start, as_of_date)
        # M19-A: 凌晨 / 盘前 today 数据还没刷, get_ohlcv_loose 可能返回 empty.
        # fallback 用上一交易日重试 (calendar 优先, 没有就裸 -1 天)
        if ohlcv.empty:
            try:
                prev_d = cal.previous_trading_day(as_of_date) if cal is not None else (as_of_date - timedelta(days=1))
                ohlcv = self.repo.get_ohlcv_loose(full.symbols, start, prev_d)
            except Exception:  # noqa: BLE001
                pass
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
                s = factor.compute(sub)  # pyright: ignore[reportArgumentType]
            except Exception:
                continue
            if s is None or s.empty:
                continue
            rows[d] = s
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).T

    def _compute_active_factor_history(self, ohlcv: pd.DataFrame, all_dates: pd.Index) -> dict[str, pd.DataFrame]:
        """计算所有 **真在用** 因子的完整 history（用于时间序列相关性筛选）。

        M19 review P0-4: registry.list_all() 现在包含 builtin + accepted + shadow.
        但相关性门槛 (max_abs_corr) 的语义是"新候选 vs 已被组合采用的因子", 不应该把
        shadow 算进来 — 否则 DSL 空间会被 39 个 shadow 锁死, 新 LLM/auto 候选
        几乎都会 corr > 0.7 被拒, 自动发现产出率持续下降。

        过滤策略: 只看 builtin + accepted, shadow/demoted 不算 "在用".
        """
        # P0-4: 查 proposal_store 拿 shadow 名字集 (builtin 不在 proposals, 自动通过)
        shadow_names: set[str] = set()
        try:
            from akq_agents.services.data.repository import open_meta_db
            db_path = getattr(self.proposal_store, "_db", None)
            if db_path is not None:
                with open_meta_db(db_path) as conn:
                    rows = conn.execute(
                        "SELECT factor_name FROM factor_proposals "
                        "WHERE status='shadow' AND evicted_at IS NULL"
                    ).fetchall()
                shadow_names = {r[0] for r in rows}
        except Exception:  # noqa: BLE001
            pass

        out: dict[str, pd.DataFrame] = {}
        for f in self.registry.list_all():
            if f.name in shadow_names:
                continue  # shadow 不算"已在用", 不参与相关性门槛
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

    def _promote_shadows(self, *, stats: DiscoveryStats, as_of_date: date) -> None:
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
            except Exception as exc:  # noqa: BLE001
                # M18-I5 followup: recipe 解析失败也写状态防 stuck
                p.oos_observations = len(oos_dates)
                p.reason = f"recipe_parse_failed: {str(exc)[:100]}"
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue
            try:
                hist = self._compute_factor_history(factor, ohlcv, all_dates)
            except Exception as exc:  # noqa: BLE001
                p.oos_observations = len(oos_dates)
                p.reason = f"compute_history_failed: {str(exc)[:100]}"
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue
            if hist.empty:
                # M18-I5: 数据稀疏时也要更新 oos_observations + reason，
                # 否则 shadow stuck 永远不 promote 也不 demote, 占用 shadow 池槽位
                p.oos_observations = len(oos_dates)
                p.reason = "data_sparse: factor history empty"
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue
            # 只看 OOS 期间
            oos_hist = hist.loc[hist.index.isin(oos_dates)]
            oos_ret = forward_returns.loc[forward_returns.index.isin(oos_dates)]
            if len(oos_hist) < 5:
                # M18-I5: 同上, OOS 样本不足也写状态防 stuck
                p.oos_observations = len(oos_dates)
                p.reason = f"data_sparse: oos_hist_len={len(oos_hist)} < 5"
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue
            # 复用 evaluator._rolling_ic 算 IR
            from akq_agents.services.portfolio.evaluator import _rolling_ic
            ic_series = _rolling_ic(oos_hist, oos_ret, window=min(len(oos_hist), 60))
            ic_clean = ic_series.dropna()
            if len(ic_clean) < 5:
                # M18-I5 followup: rolling IC 样本不足也写状态防 stuck
                p.oos_observations = len(oos_dates)
                p.reason = f"data_sparse: ic_clean_len={len(ic_clean)} < 5"
                p.evaluated_at = now_iso()
                self.proposal_store.upsert(p)
                continue
            oos_ic_mean = float(ic_clean.mean())
            oos_ic_std = float(ic_clean.std(ddof=1)) if ic_clean.std(ddof=1) > 0 else None
            oos_ir = (oos_ic_mean / oos_ic_std) if oos_ic_std else None
            # M19 review: t_stat = IR * sqrt(N), 自然带样本量归一化, 防止小样本假阳
            import math as _math
            oos_t_stat = (oos_ir * _math.sqrt(len(ic_clean))) if oos_ir is not None else None

            # M19 review: 同时检查 IR 和 t_stat — IR>=0.15 + |t_stat|>=2.0 才 promote.
            # |t_stat|<2.0 (相当于 p>0.05) 说明 OOS 这段 IR 大概率是噪音, 不该 promote 进组合。
            ir_pass = oos_ir is not None and abs(oos_ir) >= self.th.shadow_min_oos_ir
            t_pass = oos_t_stat is not None and abs(oos_t_stat) >= self.th.shadow_min_oos_t_stat
            if ir_pass and t_pass:
                assert oos_ir is not None  # ir_pass 已保证, 给 type checker
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
                stats.promoted += 1
                logger.info(
                    "discovery: shadow %s PROMOTED (oos_ir=%.3f over %d days%s)",
                    p.factor_name, oos_ir, len(oos_dates), flip_note,
                )
            else:
                # M15-A: 不立刻 demote — 看时长 + IR 决定
                # - oos_days >= shadow_max_days (60) 且 |IR| < shadow_min_keep_ir (0.10):
                #     真的不行 → demote
                # - 否则: 继续观察（更新 oos_observations / oos_ir，status 仍 'shadow'）
                ir_too_low = oos_ir is None or abs(oos_ir) < self.th.shadow_min_keep_ir
                if len(oos_dates) >= self.th.shadow_max_days and ir_too_low:
                    p.status = "demoted"
                    p.reason = (
                        f"demoted_after_{len(oos_dates)}d_oos_ir={oos_ir:.3f}"
                        if oos_ir is not None
                        else f"demoted_after_{len(oos_dates)}d_oos_ir=None"
                    )
                    p.ir = oos_ir
                    p.oos_observations = len(oos_dates)
                    p.oos_ir = oos_ir
                    p.evaluated_at = now_iso()
                    self.proposal_store.upsert(p)
                    stats.demoted += 1
                    logger.info(
                        "discovery: shadow %s DEMOTED (oos_ir=%s over %d days)",
                        p.factor_name, oos_ir, len(oos_dates),
                    )
                else:
                    # 继续观察
                    p.oos_observations = len(oos_dates)
                    p.oos_ir = oos_ir
                    p.evaluated_at = now_iso()
                    self.proposal_store.upsert(p)
                    logger.info(
                        "discovery: shadow %s 继续观察 (oos_days=%d, oos_ir=%s)",
                        p.factor_name, len(oos_dates), oos_ir,
                    )


def restore_accepted_factors(
    registry: FactorRegistry, proposal_store: FactorProposalStore
) -> int:
    """启动期：把数据库里 status='accepted' 或 'shadow' 的因子 register 到内存 registry。

    M19: 之前只 register accepted, shadow 因子永远没机会参与组合 (得等 ≥20 OOS 天数 + |IR|≥0.15
    promote 到 accepted 才行). 现在 builtin/accepted/shadow 都进 registry, 由
    CompositeScorer 用 min_abs_ir 阈值统一筛选 — 表现达标就用, 不分来源。

    rejected/demoted 不 restore (recipe 已经被人工/自动判定不行)。

    重构: 支持 recipe_kind='code' — 从 ``p.recipe_code`` 读 source, sandbox 编译成 CodeFactor.
    """
    from akq_agents.services.factors.proposal_store import recipe_from_json

    count = 0
    for p in proposal_store.list_accepted():
        # list_accepted 返回 accepted + shadow (proposal_store.py:154 注释)
        if p.status not in ("accepted", "shadow"):
            continue
        try:
            if p.recipe_kind == "code":
                # Code 路径: 从 recipe_code 字段取 source, sandbox 编译
                if not p.recipe_code:
                    logger.warning("restore: code factor %s missing recipe_code, skip",
                                   p.factor_name)
                    continue
                fn, ch = compile_code_factor(p.recipe_code, timeout_s=10.0)
                factor = CodeFactor(
                    name=p.factor_name,
                    source_code=p.recipe_code,
                    fn=fn,
                    factor_version=p.factor_name and 1 or 1,  # code 路径暂无 version 概念, 默认 1
                    direction=p.direction,
                    code_hash=ch or p.code_hash or "",
                )
            else:
                # DSL 路径 (默认)
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
