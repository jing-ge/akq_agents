"""Paper Trading 前向跟踪（P0-2）。

每天 daemon 跑完 portfolio 后：
1. 把当日权重 + 当日 close 冻结到 paper_trades（永不修改）
2. 把所有历史 paper_trades 用最新 close 估值，写到 paper_track_perf

这是把 in-sample backtest（NAV）变成 out-of-sample 证据的唯一通路。

设计原则：
- 永不修改原始 frozen_price / frozen_weight；后续 close 修正不能改 paper_trades
- 单股票按当日 close 等比例换算成"假定股数"（不取整，因为是 paper trading）
- 假定本金 = 配置项（默认 100,000 元）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_PAPER_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cohort_date TEXT NOT NULL,         -- 冻结时的"建仓日"
  symbol TEXT NOT NULL,
  frozen_weight REAL NOT NULL,
  frozen_price REAL NOT NULL,        -- 建仓日 close（永不改）
  assumed_capital REAL NOT NULL,     -- 假定本金
  assumed_shares REAL NOT NULL,      -- (weight × capital) / frozen_price，不取整
  created_at TEXT NOT NULL,
  UNIQUE(cohort_date, symbol)
);
"""

_PAPER_TRACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_track_perf (
  cohort_date TEXT NOT NULL,           -- 哪一天建的仓
  as_of_date TEXT NOT NULL,            -- 评估日
  current_value REAL NOT NULL,         -- 假定该 cohort 全部持有，今日市值
  return_pct REAL NOT NULL,            -- 相对建仓日的总收益率
  benchmark_return_pct REAL,           -- 同期沪深300 收益率
  excess_return_pct REAL,              -- 超额
  days_elapsed INTEGER NOT NULL,
  PRIMARY KEY (cohort_date, as_of_date)
);
"""

_INDEX_TRADES = "CREATE INDEX IF NOT EXISTS idx_paper_trades_cohort ON paper_trades(cohort_date);"
_INDEX_PERF = "CREATE INDEX IF NOT EXISTS idx_paper_track_as_of ON paper_track_perf(as_of_date);"


@dataclass
class PaperTradingConfig:
    assumed_capital: float = 100_000.0  # 假定本金（用于"如果我真买了"的视角）
    benchmark_symbol: str = "000300"


class PaperTradingStore:
    """冻结 + 估值。"""

    def __init__(self, meta_db_path: Path, cfg: PaperTradingConfig | None = None) -> None:
        self._db = Path(meta_db_path)
        self._cfg = cfg or PaperTradingConfig()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_PAPER_TRADES_SCHEMA)
            conn.execute(_PAPER_TRACK_SCHEMA)
            conn.execute(_INDEX_TRADES)
            conn.execute(_INDEX_PERF)
            conn.commit()

    # ------------------------------------------------------------------
    # 1) 冻结当日 cohort
    # ------------------------------------------------------------------

    def freeze_today_cohort(
        self,
        as_of_date: date,
        weights: dict[str, float],
        close_prices: dict[str, float],
        fallback_lookup=None,
    ) -> int:
        """把当日权重 + close 冻结到 paper_trades。

        已存在的 (cohort_date, symbol) 不修改（PRIMARY KEY 保证）。

        修复 oracle #2：close_prices 里缺 symbol（停牌）时，如果 fallback_lookup 不为 None，
        会调用 fallback_lookup(symbol, as_of_date) 退化用最近 close 冻结，
        避免静默丢权重导致 paper 当日总权重 < 100%、长期低估收益。

        返回新写入的行数。
        """
        from datetime import datetime

        if not weights:
            return 0

        capital = self._cfg.assumed_capital
        rows = []
        now = datetime.now().isoformat(timespec="seconds")
        for sym, w in weights.items():
            price = close_prices.get(sym)
            if (price is None or price <= 0) and fallback_lookup is not None:
                try:
                    price = fallback_lookup(sym, as_of_date)
                except Exception:
                    price = None
            if price is None or price <= 0:
                continue
            assumed_shares = (w * capital) / float(price)
            rows.append((
                as_of_date.isoformat(),
                str(sym),
                float(w),
                float(price),
                capital,
                assumed_shares,
                now,
            ))
        if not rows:
            return 0
        with open_meta_db(self._db) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO paper_trades
                  (cohort_date, symbol, frozen_weight, frozen_price,
                   assumed_capital, assumed_shares, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = conn.total_changes
            conn.commit()
        return inserted

    # ------------------------------------------------------------------
    # 2) 估值：所有历史 cohort 用最新 close 估值
    # ------------------------------------------------------------------

    def update_track_perf(
        self,
        as_of_date: date,
        latest_close: dict[str, float],
        cohort_close_lookup=None,
    ) -> dict:
        """对所有历史 cohort 用 latest_close 重新估值并写 paper_track_perf。

        latest_close: {symbol: close_at as_of_date}，含 benchmark_symbol。
        cohort_close_lookup: 可选 callable(symbol, date) -> close，用于查询历史 benchmark 收盘
            （benchmark 不在 paper_trades 表里，否则会拿不到）。
            如果为 None，benchmark 收益不算（excess_return_pct = None）。
        """
        from datetime import date as _date

        bench_close_today = latest_close.get(self._cfg.benchmark_symbol)
        with open_meta_db(self._db) as conn:
            cohorts = conn.execute(
                "SELECT DISTINCT cohort_date FROM paper_trades"
            ).fetchall()

        stats = {"cohorts": 0, "updated": 0, "skipped_no_data": 0}
        for (cd,) in cohorts:
            cohort_d = _date.fromisoformat(cd)
            if cohort_d > as_of_date:
                continue
            stats["cohorts"] += 1
            with open_meta_db(self._db) as conn:
                holdings = conn.execute(
                    """
                    SELECT symbol, frozen_weight, frozen_price,
                           assumed_capital, assumed_shares
                    FROM paper_trades WHERE cohort_date = ?
                    """,
                    (cd,),
                ).fetchall()
            if not holdings:
                continue

            total_value = 0.0
            assumed_capital = float(holdings[0][3])
            for sym, _w, fz_p, _cap, shares in holdings:
                cur_p = latest_close.get(str(sym))
                if cur_p is None or cur_p <= 0:
                    # C4 fix: 与 freeze_today_cohort 的 fallback_lookup 路径对称 —
                    # 优先查最近有效 close（停牌期间走最近一日 close 而非建仓价），
                    # 最后才退回 frozen_price 兜底。
                    if cohort_close_lookup is not None:
                        looked = cohort_close_lookup(str(sym), as_of_date)
                        if looked is not None and looked > 0:
                            cur_p = float(looked)
                    if cur_p is None or cur_p <= 0:
                        cur_p = float(fz_p)
                total_value += float(shares) * float(cur_p)

            return_pct = (total_value - assumed_capital) / assumed_capital if assumed_capital > 0 else 0.0

            # benchmark：必须依赖外部传入的 cohort_close_lookup（一般是从 ohlcv 反查）。
            # 不再 fallback 到 paper_trades 表查 frozen_price——因为 freeze 时只按组合 weights
            # 写持仓票，benchmark (000300) 永远不在 paper_trades 里，fallback 永远 None。
            bench_return_pct = None
            excess_return_pct = None
            if bench_close_today is not None and cohort_close_lookup is not None:
                bench_at_cohort = cohort_close_lookup(self._cfg.benchmark_symbol, cohort_d)
                if bench_at_cohort is not None and bench_at_cohort > 0:
                    bench_return_pct = (bench_close_today - bench_at_cohort) / bench_at_cohort
                    excess_return_pct = return_pct - bench_return_pct
            elif bench_close_today is not None and cohort_close_lookup is None:
                logger.warning(
                    "update_track_perf: cohort_close_lookup missing, benchmark excess will be None"
                )

            days_elapsed = (as_of_date - cohort_d).days
            with open_meta_db(self._db) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO paper_track_perf
                      (cohort_date, as_of_date, current_value,
                       return_pct, benchmark_return_pct, excess_return_pct,
                       days_elapsed)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cd, as_of_date.isoformat(), float(total_value),
                     float(return_pct),
                     float(bench_return_pct) if bench_return_pct is not None else None,
                     float(excess_return_pct) if excess_return_pct is not None else None,
                     int(days_elapsed)),
                )
                conn.commit()
            stats["updated"] += 1

        return stats

    # ------------------------------------------------------------------
    # 3) 读
    # ------------------------------------------------------------------

    def list_cohorts(self, limit: int = 60) -> list[dict]:
        """所有冻结过的 cohort_date，含每个 cohort 的最新表现。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT pt.cohort_date,
                       COUNT(*) AS n_holdings,
                       MAX(ptp.as_of_date) AS latest_eval,
                       (SELECT return_pct FROM paper_track_perf
                        WHERE cohort_date = pt.cohort_date
                        ORDER BY as_of_date DESC LIMIT 1) AS latest_return,
                       (SELECT excess_return_pct FROM paper_track_perf
                        WHERE cohort_date = pt.cohort_date
                        ORDER BY as_of_date DESC LIMIT 1) AS latest_excess,
                       (SELECT days_elapsed FROM paper_track_perf
                        WHERE cohort_date = pt.cohort_date
                        ORDER BY as_of_date DESC LIMIT 1) AS days
                FROM paper_trades pt
                LEFT JOIN paper_track_perf ptp ON pt.cohort_date = ptp.cohort_date
                GROUP BY pt.cohort_date
                ORDER BY pt.cohort_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "cohort_date": r[0],
                "n_holdings": r[1],
                "latest_eval_date": r[2],
                "latest_return_pct": r[3],
                "latest_excess_pct": r[4],
                "days_elapsed": r[5],
            }
            for r in rows
        ]

    def get_cohort_timeseries(self, cohort_date: str) -> list[dict]:
        """某 cohort 的逐日表现时序。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT as_of_date, current_value, return_pct,
                       benchmark_return_pct, excess_return_pct, days_elapsed
                FROM paper_track_perf
                WHERE cohort_date = ?
                ORDER BY as_of_date ASC
                """,
                (cohort_date,),
            ).fetchall()
        return [
            {
                "as_of_date": r[0],
                "current_value": r[1],
                "return_pct": r[2],
                "benchmark_return_pct": r[3],
                "excess_return_pct": r[4],
                "days_elapsed": r[5],
            }
            for r in rows
        ]

    def summary(self) -> dict:
        """汇总：所有 cohort 在 30 / 60 / 90 天后的平均表现。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT days_elapsed, return_pct, excess_return_pct
                FROM paper_track_perf
                """
            ).fetchall()
        if not rows:
            return {"n_cohorts": 0, "n_evaluations": 0}

        df = pd.DataFrame(rows, columns=["days", "ret", "excess"])
        df["ret"] = pd.to_numeric(df["ret"], errors="coerce")
        df["excess"] = pd.to_numeric(df["excess"], errors="coerce")

        def at(days_target: int) -> dict:
            sub = df[(df["days"] >= days_target - 5) & (df["days"] <= days_target + 5)]
            if sub.empty:
                return {"n": 0}
            return {
                "n": len(sub),
                "mean_return": float(sub["ret"].mean()),
                "median_return": float(sub["ret"].median()),
                "win_rate": float((sub["ret"] > 0).mean()),
                "mean_excess": float(sub["excess"].dropna().mean()) if sub["excess"].notna().any() else None,
                "excess_win_rate": float((sub["excess"] > 0).mean()) if sub["excess"].notna().any() else None,
            }

        with open_meta_db(self._db) as conn:
            n_cohorts = conn.execute("SELECT COUNT(DISTINCT cohort_date) FROM paper_trades").fetchone()[0]

        return {
            "n_cohorts": int(n_cohorts),
            "n_evaluations": int(len(df)),
            "at_30d": at(30),
            "at_60d": at(60),
            "at_90d": at(90),
        }
