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
  status TEXT NOT NULL,          -- accepted | shadow | rejected | pending | demoted
  ic_mean REAL,
  ic_std REAL,
  ir REAL,
  t_stat REAL,
  max_abs_corr REAL,             -- 与已 active 因子的最大绝对相关系数
  reason TEXT,                   -- 拒绝原因或 'ok'
  created_at TEXT NOT NULL,
  evaluated_at TEXT,
  -- M7-C 新增字段（往后兼容；旧记录默认 NULL）
  shadow_started_at TEXT,        -- 进入 shadow 的时间
  oos_observations INTEGER,      -- OOS 观察的交易日数（>=N 才 promote）
  oos_ir REAL                    -- OOS 期间的 IR
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
    shadow_started_at: str | None = None
    oos_observations: int | None = None
    oos_ir: float | None = None


class FactorProposalStore:
    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            # M7-C 增量加列（老库兼容）
            cur = conn.execute("PRAGMA table_info(factor_proposals)")
            existing_cols = {row[1] for row in cur.fetchall()}
            for col, ddl in [
                ("shadow_started_at", "ALTER TABLE factor_proposals ADD COLUMN shadow_started_at TEXT"),
                ("oos_observations", "ALTER TABLE factor_proposals ADD COLUMN oos_observations INTEGER"),
                ("oos_ir", "ALTER TABLE factor_proposals ADD COLUMN oos_ir REAL"),
            ]:
                if col not in existing_cols:
                    conn.execute(ddl)
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
                   created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name) DO UPDATE SET
                  status=excluded.status,
                  ic_mean=excluded.ic_mean,
                  ic_std=excluded.ic_std,
                  ir=excluded.ir,
                  t_stat=excluded.t_stat,
                  max_abs_corr=excluded.max_abs_corr,
                  reason=excluded.reason,
                  evaluated_at=excluded.evaluated_at,
                  shadow_started_at=COALESCE(excluded.shadow_started_at, factor_proposals.shadow_started_at),
                  oos_observations=excluded.oos_observations,
                  oos_ir=excluded.oos_ir
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
                    proposal.shadow_started_at,
                    proposal.oos_observations,
                    proposal.oos_ir,
                ),
            )
            conn.commit()

    def list_accepted(self) -> list[FactorProposal]:
        """已晋升 / shadow 的因子（status in (accepted, shadow)）—— 都进内存 registry。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT factor_name, recipe_json, direction, status,
                       ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                       created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir
                FROM factor_proposals
                WHERE status IN ('accepted', 'shadow')
                ORDER BY evaluated_at DESC
                """
            ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def list_shadow(self) -> list[FactorProposal]:
        """正在 OOS 观察的 shadow 因子（每轮 discovery 复评、N 天后 promote）。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT factor_name, recipe_json, direction, status,
                       ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                       created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir
                FROM factor_proposals
                WHERE status = 'shadow'
                ORDER BY shadow_started_at ASC
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
                           created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir
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
                           created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir
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
