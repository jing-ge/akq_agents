"""FactorEvaluator + factor_metrics 表读写。

P3a 仅做基础统计：
- rolling IC (Spearman 相关系数 between factor_t 与 forward_return_{t+1})
- IR = mean(IC) / std(IC)
- t-stat = IR * sqrt(N)

仅用于可观测：``status='active'`` 永远写入（P3a 不基于 metrics 做失能）。
P3b 起：根据连续 N 周 IR 退化判定 'inactive'，CompositeScorer 才会读 metrics 做权重。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from akq_agents.services.data.repository import open_meta_db
from akq_agents.services.factors.base import Factor

logger = logging.getLogger(__name__)


_FACTOR_METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_name TEXT NOT NULL,
  factor_version INTEGER NOT NULL,
  as_of_date TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  ic_mean REAL,
  ic_std REAL,
  ir REAL,
  t_stat REAL,
  status TEXT NOT NULL,
  reason TEXT,
  UNIQUE(factor_name, factor_version, as_of_date, window_days)
);
"""

_FACTOR_METRICS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_factor_metrics_lookup
  ON factor_metrics(factor_name, factor_version, as_of_date);
"""


@dataclass
class FactorMetric:
    factor_name: str
    factor_version: int
    as_of_date: str
    window_days: int
    ic_mean: float | None
    ic_std: float | None
    ir: float | None
    t_stat: float | None
    status: str
    reason: str | None


def _effective_sample_size(ic_series: pd.Series) -> float:
    """IC 序列的有效样本量 N_eff = N * (1-ρ)/(1+ρ), ρ 为一阶自相关。

    逐日 Spearman IC 高度自相关 (因子值日间持续), 直接用 N 算 t_stat=IR*sqrt(N)
    会系统性高估显著性 → 大量噪音因子被误判显著而 promote。用 AR(1) 近似的有效
    样本量修正: ρ>0 (正自相关, 常见) 时 N_eff < N, t_stat 相应缩小。

    - ρ 只取正值方向的修正 (clip 到 [0, 0.99)); ρ<=0 时不放大, N_eff=N。
    - N<3 无法估自相关, 直接返回 N。
    返回浮点有效样本量, 调用方用 sqrt(N_eff) 替代 sqrt(N)。
    """
    ic = ic_series.dropna()
    n = len(ic)
    if n < 3:
        return float(n)
    # 一阶自相关 ρ = corr(ic[t], ic[t-1])
    rho = float(ic.autocorr(lag=1)) if ic.std(ddof=1) > 0 else 0.0
    if not np.isfinite(rho) or rho <= 0:
        return float(n)
    rho = min(rho, 0.99)  # 防除零/爆炸
    n_eff = n * (1.0 - rho) / (1.0 + rho)
    # 至少保留 2, 避免极端自相关把有效样本压到 <1 使 t_stat 失真
    return max(2.0, n_eff)


def _rolling_ic(
    factor_history: pd.DataFrame,
    forward_returns: pd.DataFrame,
    window: int,
) -> pd.Series:
    """计算 rolling Spearman IC。

    Args:
        factor_history: index=date, columns=symbol, values=因子值。
        forward_returns: index=date, columns=symbol, values=下一日 return。
        window: 滚动窗口大小

    Returns:
        index=date 的 IC 序列；取最后 ``window`` 天的逐日 IC。
    """
    aligned_idx = factor_history.index.intersection(forward_returns.index)
    if len(aligned_idx) < window:
        return pd.Series(dtype=float)
    f = factor_history.loc[aligned_idx].tail(window)
    r = forward_returns.loc[aligned_idx].tail(window)
    ic_series = []
    for d in f.index:
        f_row = f.loc[d]
        r_row = r.loc[d]
        # 转 numpy series 兼容 pyright
        try:
            f_s = pd.Series(f_row).dropna()
            r_s = pd.Series(r_row).dropna()
        except (TypeError, ValueError):
            ic_series.append(np.nan)
            continue
        common = f_s.index.intersection(r_s.index)
        if len(common) < 3:
            ic_series.append(np.nan)
            continue
        # Spearman = Pearson of ranks
        ic = f_s.loc[common].rank().corr(r_s.loc[common].rank())
        ic_series.append(ic)
    return pd.Series(ic_series, index=f.index, dtype=float)


def _rolling_ic_full(
    factor_history: pd.DataFrame,
    forward_returns: pd.DataFrame,
) -> pd.Series:
    """M22: 一次算完所有日期的逐日 Spearman IC，返回完整 series。

    之前 evaluate 90 次每次重算同一 60 天 IC，浪费 90x CPU。
    新 API 给 batch backfill 用：对一个因子算 1 次 90 天 IC series，
    然后取 tail(60) 算 mean/std/IR/t_stat 各 90 次（O(1) 每次）。
    """
    aligned_idx = factor_history.index.intersection(forward_returns.index)
    if len(aligned_idx) < 3:
        return pd.Series(dtype=float)
    f = factor_history.loc[aligned_idx]
    r = forward_returns.loc[aligned_idx]
    ic_series = []
    ic_index = []
    for d in f.index:
        f_row = f.loc[d]
        r_row = r.loc[d]
        try:
            f_s = pd.Series(f_row).dropna()
            r_s = pd.Series(r_row).dropna()
        except (TypeError, ValueError):
            ic_series.append(np.nan)
            ic_index.append(d)
            continue
        common = f_s.index.intersection(r_s.index)
        if len(common) < 3:
            ic_series.append(np.nan)
            ic_index.append(d)
            continue
        ic_series.append(f_s.loc[common].rank().corr(r_s.loc[common].rank()))
        ic_index.append(d)
    return pd.Series(ic_series, index=ic_index, dtype=float)


class FactorEvaluator:
    """对每个因子计算滚动 IC / IR / t-stat 并写 factor_metrics 表。"""

    def __init__(self, meta_db_path: Path, window: int = 60) -> None:
        self._db = Path(meta_db_path)
        self._window = window
        # M22: evaluate_batch / batch() context manager 的 buffer + flag.
        # 关键: 必须 thread-local. 8 worker 共享 evaluator 实例时, 共享 buffer 会导致
        # 互相看到对方的 metrics, 互相 flush, 状态完全乱. history_backfill 在 batch_deep_research
        # 8 worker pool 下跑必须 thread-local 才能安全.
        import threading
        self._tls = threading.local()
        self._ensure_schema()

    def _pending_metrics(self) -> list:
        if not hasattr(self._tls, "pending"):
            self._tls.pending = []
        return self._tls.pending

    def _in_batch(self) -> bool:
        return getattr(self._tls, "in_batch", False)

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_FACTOR_METRICS_SCHEMA)
            conn.execute(_FACTOR_METRICS_INDEX)
            conn.commit()

    def evaluate(
        self,
        *,
        factor: Factor,
        factor_history: pd.DataFrame,
        forward_returns: pd.DataFrame,
        as_of_date: date,
    ) -> FactorMetric:
        """对一个 factor 跑一轮评估并写入。

        ``factor_history``：index=date, columns=symbol, values=该 factor 的历史值。
        ``forward_returns``：同 shape，下一日收益（close.pct_change().shift(-1)）。
        """
        ic = _rolling_ic(factor_history, forward_returns, self._window)
        ic_clean = ic.dropna()
        if len(ic_clean) < 5:
            metric = FactorMetric(
                factor_name=factor.name,
                factor_version=factor.factor_version,
                as_of_date=as_of_date.isoformat(),
                window_days=self._window,
                ic_mean=None,
                ic_std=None,
                ir=None,
                t_stat=None,
                status="active",
                reason="insufficient_data",
            )
        else:
            ic_mean = float(ic_clean.mean())
            ic_std = float(ic_clean.std(ddof=1))
            ir = ic_mean / ic_std if ic_std > 0 else None
            # M19+P3: t_stat 用有效样本量 N_eff (修正 IC 自相关导致的显著性高估)
            t_stat = ir * np.sqrt(_effective_sample_size(ic_clean)) if ir is not None else None
            # M3 + 改进：单点低 IR 不立即 disable，需要"连续 N 期"低才标 inactive
            # 避免单日数据精度问题（如 spot 数据）导致核心因子瞬间被 disable
            status = "active"
            reason: str | None = None
            if ir is None or abs(ir) < 0.15:
                # 看历史最近 4 期，加上当前共 5 期；如果 ≥3 期 |IR|<0.15 才 inactive
                recent = self._read_recent_history(factor.name, factor.factor_version, limit=4)
                low_count = 1 if (ir is None or abs(ir) < 0.15) else 0
                for m in recent:
                    if m.ir is None or abs(m.ir) < 0.15:
                        low_count += 1
                if low_count >= 3:
                    status = "inactive"
                    reason = "low_ir_persistent"
                else:
                    reason = f"low_ir_observed_{low_count}/5"
                    # 保持 active，给因子缓冲
            metric = FactorMetric(
                factor_name=factor.name,
                factor_version=factor.factor_version,
                as_of_date=as_of_date.isoformat(),
                window_days=self._window,
                ic_mean=ic_mean,
                ic_std=ic_std,
                ir=ir,
                t_stat=t_stat,
                status=status,
                reason=reason,
            )
        # M22: 写入 thread-local buffer. 默认 evaluate 单调走单 commit. history_backfill 在
        # batch() context 内累积 buffer 不 flush, context exit 时一次 _upsert_many.
        # 8 worker 共享 evaluator 时, thread-local 隔离各 worker 的 buffer.
        buf = self._pending_metrics()
        buf.append(metric)
        if not self._in_batch():
            if buf:
                self._upsert_many(buf)
                self._tls.pending = []
        return metric

    @contextmanager
    def batch(self):
        """M22: 进入 batch 模式, evaluate 内部 _pending_metrics 累积, 退出时一次 _upsert_many.

        适合 history_backfill (90 天 1 因子) / batch_deep_research 之后需要 N 行同写场景.
        异常路径自动 flush 已累积的 (best-effort), 仍抛原异常.

        Thread-local: 同一 evaluator 实例在 8 worker 下各自独立 buffer, 互不干扰.
        """
        prev = self._in_batch()
        self._tls.in_batch = True
        try:
            yield
        except BaseException:
            # 异常前 flush 已写入的, 不让脏数据卡住下次 batch
            buf = self._pending_metrics()
            if buf:
                try:
                    self._upsert_many(buf)
                except Exception:
                    pass
                self._tls.pending = []
            raise
        finally:
            self._tls.in_batch = prev
            buf = self._pending_metrics()
            if not self._in_batch() and buf:
                self._upsert_many(buf)
                self._tls.pending = []

    def evaluate_batch(
        self,
        *,
        factor: Factor,
        factor_history: pd.DataFrame,
        forward_returns: pd.DataFrame,
        as_of_dates: list,
    ) -> list:
        """M22: 批量评估. 内部按日期循环 evaluate 逻辑 (status 判定要最新历史), 但累积
        metrics 不立即写, 结束后 _upsert_many 一次 commit.

        与 evaluate() 区别: evaluate() 写 1 行 1 commit (默认); evaluate_batch() 写 N 行 1 commit.
        history_backfill.py 90 天回填走这个, 锁争用从 90x 降到 1x.

        重要: 每个 as_of_date 的 status 判定依赖"前 N 期的历史", 所以中途不能
        _upsert_many (会改变 _read_recent_history 结果). 严格串行执行.
        """
        results: list = []
        # 局部 buffer 累积本次批量的 metrics; 循环结束一次性 _upsert_many commit.
        # (不用 self._pending_metrics() thread-local buffer: 那是 batch() context
        #  manager 的语义, 与本方法"严格串行 + 单次 flush"独立, 混用会互相污染)
        pending: list = []
        for as_of_date in as_of_dates:
            fh_sub = factor_history.loc[:as_of_date]
            fr_sub = forward_returns.loc[:as_of_date]
            common_idx = fh_sub.index.intersection(fr_sub.index)
            if len(common_idx) < self._window:
                metric = FactorMetric(
                    factor_name=factor.name,
                    factor_version=factor.factor_version,
                    as_of_date=as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date),
                    window_days=self._window,
                    ic_mean=None, ic_std=None, ir=None, t_stat=None,
                    status="active", reason="insufficient_data",
                )
            else:
                metric = self._compute_metric(
                    factor=factor,
                    factor_history=fh_sub.loc[common_idx],
                    forward_returns=fr_sub.loc[common_idx],
                    as_of_date=as_of_date,
                )
            # 不立即写, 暂存到局部 buffer
            pending.append(metric)
            results.append(metric)
        # 一次 commit (无异常时才写; 异常直接向上抛, 不残留半批)
        if pending:
            self._upsert_many(pending)
        return results

    def evaluate_batch_fast(
        self,
        *,
        factor: Factor,
        factor_history: pd.DataFrame,
        forward_returns: pd.DataFrame,
        as_of_dates: list,
    ) -> list:
        """方案 2 提速: 与 evaluate_batch 数值严格等价, 但把 rolling IC 增量化。

        evaluate_batch 对每个 as_of 都调 _rolling_ic (tail(window) 逐日重算, 相邻窗口
        59/60 重叠 → ~97% 冗余)。本方法只调一次 _rolling_ic_full 得全历史逐日 IC,
        再对每个 as_of 取其对齐窗口内的 tail(window) 做 O(1) 聚合。

        等价性: 逐日 IC 只依赖当天截面 rank 相关, 与窗口/截止日无关, 故
        _rolling_ic(fh.loc[:as_of], fr.loc[:as_of], w) 与 full_ic 落在同一批日期上的值
        逐位相等。status 判定同样走 _read_recent_history (buffer 结束才 flush, 与
        evaluate_batch 读到的 db 状态一致)。见 tests/portfolio/test_rolling_ic_incremental_equiv.py。

        串行约束不变: status 依赖前 N 期历史, 中途不 _upsert_many, 结束一次写。
        """
        # 全历史逐日 IC 只算一次 (index = fh ∩ fr 的日期)
        full_ic = _rolling_ic_full(factor_history, forward_returns)

        results: list = []
        pending: list = []
        for as_of_date in as_of_dates:
            fh_sub = factor_history.loc[:as_of_date]
            fr_sub = forward_returns.loc[:as_of_date]
            common_idx = fh_sub.index.intersection(fr_sub.index)
            if len(common_idx) < self._window:
                metric = FactorMetric(
                    factor_name=factor.name,
                    factor_version=factor.factor_version,
                    as_of_date=as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date),
                    window_days=self._window,
                    ic_mean=None, ic_std=None, ir=None, t_stat=None,
                    status="active", reason="insufficient_data",
                )
            else:
                # 与旧路径对齐: _rolling_ic(fh.loc[common_idx], ...) 取 common_idx 上最后
                # window 天。full_ic 已是 fh∩fr 全历史 IC, 取 common_idx ∩ full_ic.index
                # 再 tail(window) 即同一批日期的同一批 IC 值。
                ic_idx = common_idx.intersection(full_ic.index)
                ic_window = full_ic.loc[ic_idx].tail(self._window)
                metric = self._metric_from_ic(
                    factor=factor, as_of_date=as_of_date, ic=ic_window
                )
            pending.append(metric)
            results.append(metric)
        if pending:
            self._upsert_many(pending)
        return results

    def _compute_metric(
        self,
        *,
        factor: Factor,
        factor_history: pd.DataFrame,
        forward_returns: pd.DataFrame,
        as_of_date,
    ) -> FactorMetric:
        """M22: evaluate() 的纯计算部分, 不写 db, 供 evaluate_batch 复用."""
        ic = _rolling_ic(factor_history, forward_returns, self._window)
        return self._metric_from_ic(factor=factor, as_of_date=as_of_date, ic=ic)

    def _metric_from_ic(
        self,
        *,
        factor: Factor,
        as_of_date,
        ic: pd.Series,
    ) -> FactorMetric:
        """从一段逐日 IC series 聚合出 FactorMetric (mean/std/ir/t_stat + status 判定)。

        纯聚合, 不写 db。供三条路径共用, 保证逻辑不漂移:
        - _compute_metric (旧 evaluate_batch)
        - evaluate (旧单点路径, 见其内联实现)
        - evaluate_batch_fast (新增量路径)

        ``ic`` 已是"截止 as_of 的窗口内逐日 IC"(旧路径 _rolling_ic tail(window),
        新路径 _rolling_ic_full.loc[:as_of].tail(window)) — 两者逐位相等, 故本函数
        对两路径产出完全一致的 metric。
        """
        as_of_iso = as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date)
        ic_clean = ic.dropna()
        if len(ic_clean) < 5:
            return FactorMetric(
                factor_name=factor.name,
                factor_version=factor.factor_version,
                as_of_date=as_of_iso,
                window_days=self._window,
                ic_mean=None, ic_std=None, ir=None, t_stat=None,
                status="active", reason="insufficient_data",
            )
        ic_mean = float(ic_clean.mean())
        ic_std = float(ic_clean.std(ddof=1))
        ir = ic_mean / ic_std if ic_std > 0 else None
        # M19+P3: t_stat 用有效样本量 N_eff (修正 IC 自相关导致的显著性高估)
        t_stat = ir * np.sqrt(_effective_sample_size(ic_clean)) if ir is not None else None
        status = "active"
        reason: str | None = None
        if ir is None or abs(ir) < 0.15:
            recent = self._read_recent_history(factor.name, factor.factor_version, limit=4)
            low_count = 1 if (ir is None or abs(ir) < 0.15) else 0
            for m in recent:
                if m.ir is None or abs(m.ir) < 0.15:
                    low_count += 1
            if low_count >= 3:
                status = "inactive"
                reason = "low_ir_persistent"
            else:
                reason = f"low_ir_observed_{low_count}/5"
        return FactorMetric(
            factor_name=factor.name,
            factor_version=factor.factor_version,
            as_of_date=as_of_iso,
            window_days=self._window,
            ic_mean=ic_mean, ic_std=ic_std, ir=ir, t_stat=t_stat,
            status=status, reason=reason,
        )

    def _upsert(self, metric: FactorMetric) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                INSERT INTO factor_metrics
                  (factor_name, factor_version, as_of_date, window_days,
                   ic_mean, ic_std, ir, t_stat, status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name, factor_version, as_of_date, window_days)
                DO UPDATE SET
                    ic_mean=excluded.ic_mean,
                    ic_std=excluded.ic_std,
                    ir=excluded.ir,
                    t_stat=excluded.t_stat,
                    status=excluded.status,
                    reason=excluded.reason
                """,
                (
                    metric.factor_name,
                    metric.factor_version,
                    metric.as_of_date,
                    metric.window_days,
                    metric.ic_mean,
                    metric.ic_std,
                    metric.ir,
                    metric.t_stat,
                    metric.status,
                    metric.reason,
                ),
            )
            conn.commit()

    def _upsert_many(self, metrics: list[FactorMetric]) -> None:
        """M22: 批量 upsert。1 个因子 90 天 metrics 一次事务, vs 之前 90 次单事务.

        单事务减少 90x 的 BEGIN/COMMIT 开销 + 减少 sqlite WAL 写锁切换,
        这是 web 卡死的关键缓解 (写时锁争用从 ~90 次降到 1 次).
        """
        if not metrics:
            return
        rows = [
            (
                m.factor_name, m.factor_version, m.as_of_date, m.window_days,
                m.ic_mean, m.ic_std, m.ir, m.t_stat, m.status, m.reason,
            )
            for m in metrics
        ]
        with open_meta_db(self._db) as conn:
            conn.executemany(
                """
                INSERT INTO factor_metrics
                  (factor_name, factor_version, as_of_date, window_days,
                   ic_mean, ic_std, ir, t_stat, status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name, factor_version, as_of_date, window_days)
                DO UPDATE SET
                    ic_mean=excluded.ic_mean,
                    ic_std=excluded.ic_std,
                    ir=excluded.ir,
                    t_stat=excluded.t_stat,
                    status=excluded.status,
                    reason=excluded.reason
                """,
                rows,
            )
            conn.commit()

    def _read_recent_history(self, factor_name: str, factor_version: int, limit: int = 4) -> list[FactorMetric]:
        """读最近 N 条历史（不含当前正在写入的）。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT factor_name, factor_version, as_of_date, window_days,
                       ic_mean, ic_std, ir, t_stat, status, reason
                FROM factor_metrics
                WHERE factor_name = ? AND factor_version = ?
                ORDER BY as_of_date DESC LIMIT ?
                """,
                (factor_name, factor_version, limit),
            ).fetchall()
        return [FactorMetric(*r) for r in rows]

    def get_latest(self, factor_name: str, factor_version: int) -> FactorMetric | None:
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                """
                SELECT factor_name, factor_version, as_of_date, window_days,
                       ic_mean, ic_std, ir, t_stat, status, reason
                FROM factor_metrics
                WHERE factor_name = ? AND factor_version = ?
                ORDER BY as_of_date DESC LIMIT 1
                """,
                (factor_name, factor_version),
            ).fetchone()
        return None if row is None else FactorMetric(*row)

    def list_history(
        self, factor_name: str, *, limit: int = 120, as_of_filter: str | None = None
    ) -> list[FactorMetric]:
        """列出某 factor 的历史 metrics（跨 version 都返回，调用方按 factor_version 分组渲染）。

        as_of_filter: 可选 ISO 日期 (YYYY-MM-DD)。如指定, 只返回 as_of_date < 此日期的 metrics,
            用于回填历史回测时避免用"未来 IR"算"历史 IR-EWMA 加权"(M19 修 lookahead bias).
        """
        with open_meta_db(self._db) as conn:
            if as_of_filter is not None:
                rows = conn.execute(
                    """
                    SELECT factor_name, factor_version, as_of_date, window_days,
                           ic_mean, ic_std, ir, t_stat, status, reason
                    FROM factor_metrics
                    WHERE factor_name = ? AND as_of_date < ?
                    ORDER BY factor_version DESC, as_of_date DESC
                    LIMIT ?
                    """,
                    (factor_name, as_of_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT factor_name, factor_version, as_of_date, window_days,
                           ic_mean, ic_std, ir, t_stat, status, reason
                    FROM factor_metrics
                    WHERE factor_name = ?
                    ORDER BY factor_version DESC, as_of_date DESC
                    LIMIT ?
                    """,
                    (factor_name, limit),
                ).fetchall()
        return [FactorMetric(*r) for r in rows]
