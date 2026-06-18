"""因子候选 SQLite 仓库 `factor_proposals` + `factor_registry_persist`。

存放：
- 所有自动发现引擎生成过的候选 recipe 与评估结果（accepted / rejected / pending）；
- accepted 因子的元信息（启动期 daemon 据此恢复内存注册表）。

写在 `meta.db`，与现有 factor_metrics 表同库。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_name TEXT NOT NULL UNIQUE,
  recipe_json TEXT NOT NULL,
  direction TEXT NOT NULL,
  status TEXT NOT NULL,          -- accepted | rejected | pending
  ic_mean REAL,
  ic_std REAL,
  ir REAL,
  t_stat REAL,
  max_abs_corr REAL,             -- 与已 accepted 因子的最大绝对相关系数
  reason TEXT,                   -- 拒绝原因或 'ok'
  created_at TEXT NOT NULL,
  evaluated_at TEXT
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_factor_proposals_status_created
  ON factor_proposals(status, created_at DESC);
"""


@dataclass
class FactorProposal:
    factor_name: str
    recipe_json: str
    direction: str
    status: str
    ic_mean: float | None
    ic_std: float | None
    ir: float | None
    t_stat: float | None
    max_abs_corr: float | None
    reason: str | None
    created_at: str
    evaluated_at: str | None


class FactorProposalStore:
    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()

    def exists(self, factor_name: str) -> bool:
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                "SELECT 1 FROM factor_proposals WHERE factor_name = ? LIMIT 1",
                (factor_name,),
            ).fetchone()
        return row is not None

    def upsert(self, proposal: FactorProposal) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                INSERT INTO factor_proposals
                  (factor_name, recipe_json, direction, status,
                   ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                   created_at, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name) DO UPDATE SET
                  status=excluded.status,
                  ic_mean=excluded.ic_mean,
                  ic_std=excluded.ic_std,
                  ir=excluded.ir,
                  t_stat=excluded.t_stat,
                  max_abs_corr=excluded.max_abs_corr,
                  reason=excluded.reason,
                  evaluated_at=excluded.evaluated_at
                """,
                (
                    proposal.factor_name,
                    proposal.recipe_json,
                    proposal.direction,
                    proposal.status,
                    proposal.ic_mean,
                    proposal.ic_std,
                    proposal.ir,
                    proposal.t_stat,
                    proposal.max_abs_corr,
                    proposal.reason,
                    proposal.created_at,
                    proposal.evaluated_at,
                ),
            )
            conn.commit()

    def list_accepted(self) -> list[FactorProposal]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT factor_name, recipe_json, direction, status,
                       ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                       created_at, evaluated_at
                FROM factor_proposals
                WHERE status = 'accepted'
                ORDER BY evaluated_at DESC
                """
            ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def list_recent(self, *, limit: int = 50, status: str | None = None) -> list[FactorProposal]:
        with open_meta_db(self._db) as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT factor_name, recipe_json, direction, status,
                           ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                           created_at, evaluated_at
                    FROM factor_proposals
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT factor_name, recipe_json, direction, status,
                           ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                           created_at, evaluated_at
                    FROM factor_proposals
                    WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def counts(self) -> dict[str, int]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM factor_proposals GROUP BY status"
            ).fetchall()
        return {status: count for status, count in rows}


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def recipe_to_json(recipe: dict) -> str:
    return json.dumps(recipe, sort_keys=True, ensure_ascii=False)


def recipe_from_json(s: str) -> dict:
    return json.loads(s)
