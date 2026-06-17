"""PortfolioSnapshotStore：``portfolio_snapshots`` 表读写。

P3 spec §2 表 DDL：含 name / industry / top_factors_json / prev_weight。
P5 直接 SELECT 渲染，不 join 其他表（P3 附录 B §1 承诺）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from akq_agents.services.data.repository import open_meta_db
from akq_agents.services.portfolio.attributor import AttributionResult

_PORTFOLIO_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,
  industry TEXT,
  weight REAL NOT NULL,
  prev_weight REAL,
  composite_score REAL,
  top_factors_json TEXT,
  UNIQUE(as_of_date, symbol)
);
"""

_PORTFOLIO_SNAPSHOTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_portfolio_date ON portfolio_snapshots(as_of_date);"
)


@dataclass
class PortfolioRow:
    as_of_date: str
    symbol: str
    name: str
    industry: str
    weight: float
    prev_weight: float | None
    composite_score: float | None
    top_factors_json: str | None


class PortfolioSnapshotStore:
    """portfolio_snapshots 表读写。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_PORTFOLIO_SNAPSHOTS_SCHEMA)
            conn.execute(_PORTFOLIO_SNAPSHOTS_INDEX)
            conn.commit()

    def write(
        self,
        *,
        as_of_date: date,
        weights: pd.Series,
        composite_score: pd.Series,
        attribution: AttributionResult,
        prev_weights: pd.Series | None = None,
        name_map: dict[str, str] | None = None,
        industry_map: dict[str, str] | None = None,
    ) -> int:
        """对 weights.index 的每只股票写一行 portfolio_snapshots。

        新股 prev_weight = 0；退市股不出现在本次 weights，所以也不在表中。
        返回写入行数。
        """
        if weights.empty:
            return 0
        names = name_map or {}
        industries = industry_map or {}
        prev = prev_weights if prev_weights is not None else pd.Series(dtype=float)
        rows = []
        for sym in weights.index:
            sym_str = str(sym)
            top_factors = attribution.per_stock.get(sym_str, [])
            prev_w = float(prev.get(sym, 0.0) or 0.0) if not prev.empty else 0.0
            score = float(composite_score.get(sym, 0.0) or 0.0)
            rows.append(
                (
                    as_of_date.isoformat(),
                    sym_str,
                    names.get(sym_str, ""),
                    industries.get(sym_str, ""),
                    float(weights[sym]),
                    prev_w,
                    score,
                    json.dumps(top_factors, ensure_ascii=False),
                )
            )
        with open_meta_db(self._db) as conn:
            conn.executemany(
                """
                INSERT INTO portfolio_snapshots
                  (as_of_date, symbol, name, industry, weight, prev_weight, composite_score, top_factors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(as_of_date, symbol) DO UPDATE SET
                    name=excluded.name,
                    industry=excluded.industry,
                    weight=excluded.weight,
                    prev_weight=excluded.prev_weight,
                    composite_score=excluded.composite_score,
                    top_factors_json=excluded.top_factors_json
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def read_prev_weights(self, as_of_date: date) -> pd.Series:
        """读取最近一次 < as_of_date 的快照作为 prev_weights。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT symbol, weight FROM portfolio_snapshots
                WHERE as_of_date = (
                    SELECT MAX(as_of_date) FROM portfolio_snapshots WHERE as_of_date < ?
                )
                """,
                (as_of_date.isoformat(),),
            ).fetchall()
        if not rows:
            return pd.Series(dtype=float)
        s = pd.Series({sym: w for sym, w in rows}, dtype=float)
        return s

    def read_snapshot(self, as_of_date: date) -> list[PortfolioRow]:
        """读取某日组合快照，按 weight 降序。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT as_of_date, symbol, name, industry, weight, prev_weight,
                       composite_score, top_factors_json
                FROM portfolio_snapshots
                WHERE as_of_date = ?
                ORDER BY weight DESC
                """,
                (as_of_date.isoformat(),),
            ).fetchall()
        return [PortfolioRow(*r) for r in rows]

    def list_dates(self, *, limit: int = 30) -> list[str]:
        """返回有快照的日期列表（DESC）。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT DISTINCT as_of_date FROM portfolio_snapshots ORDER BY as_of_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(r[0]) for r in rows]
