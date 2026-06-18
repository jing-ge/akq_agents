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


class FactorEvaluator:
    """对每个因子计算滚动 IC / IR / t-stat 并写 factor_metrics 表。"""

    def __init__(self, meta_db_path: Path, window: int = 60) -> None:
        self._db = Path(meta_db_path)
        self._window = window
        self._ensure_schema()

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
            t_stat = ir * np.sqrt(len(ic_clean)) if ir is not None else None
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
        self._upsert(metric)
        return metric

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
        self, factor_name: str, *, limit: int = 120
    ) -> list[FactorMetric]:
        """列出某 factor 的历史 metrics（跨 version 都返回，调用方按 factor_version 分组渲染）。"""
        with open_meta_db(self._db) as conn:
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
