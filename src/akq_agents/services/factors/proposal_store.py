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
  recipe_kind TEXT NOT NULL DEFAULT 'dsl',  -- dsl | code: 描述 recipe_json 含义
                                            --   dsl: 4-tuple {base,op,window,direction}
                                            --   code: source_code 走 sandbox 执行
  recipe_json TEXT NOT NULL,
  direction TEXT NOT NULL,
  status TEXT NOT NULL,          -- accepted | shadow | rejected | pending | demoted | llm_suggested
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
  oos_ir REAL,                   -- OOS 期间的 IR
  -- 重构新增: 自由代码路径 (recipe_kind='code') 走 source_code 字符串
  recipe_code TEXT,              -- 仅 recipe_kind='code' 写, LLM 出的 Python source
  code_hash TEXT                 -- recipe_code 的 sha1, 用于 code 因子跨 LLM-session 去重
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_factor_proposals_status_created
  ON factor_proposals(status, created_at DESC);
"""


@dataclass
class FactorProposal:
    # 字段顺序必须和 FactorProposalStore._SELECT_COLS 保持一致,
    # 否则 list_recent 用 FactorProposal(*r) 解构时 recipe_kind 错位变 None.
    # 所有字段都给默认值, 方便位置参数 / 关键字参数混用.
    factor_name: str = ""
    recipe_kind: str = "dsl"  # 重构: dsl | code
    recipe_json: str = ""
    direction: str = "long"
    status: str = "pending"
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    t_stat: float | None = None
    max_abs_corr: float | None = None
    reason: str | None = None
    created_at: str = ""
    evaluated_at: str | None = None
    shadow_started_at: str | None = None
    oos_observations: int | None = None
    oos_ir: float | None = None
    recipe_code: str | None = None  # 重构: 仅 code 路径写
    code_hash: str | None = None   # 重构: recipe_code 的 sha1


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
                # M19 review P0-2: 软删除 — 淘汰因子标 evicted_at 而非物理 DELETE,
                # 保留 factor_metrics 历史让 portfolio_snapshots 老快照仍可解释
                ("evicted_at", "ALTER TABLE factor_proposals ADD COLUMN evicted_at TEXT"),
                # 重构: 区分 DSL 候选 vs LLM-code 自由候选
                ("recipe_kind", "ALTER TABLE factor_proposals ADD COLUMN recipe_kind TEXT NOT NULL DEFAULT 'dsl'"),
                ("recipe_code", "ALTER TABLE factor_proposals ADD COLUMN recipe_code TEXT"),
                ("code_hash", "ALTER TABLE factor_proposals ADD COLUMN code_hash TEXT"),
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

    def exists_recipe(self, recipe_json: str) -> str | None:
        """按 recipe 内容查重 (跨 auto_/llm_ 命名空间, 仅 DSL 路径)。

        返回已存在的 factor_name (任意一个), 没有则 None。
        用途: DSL brainstormer 提议时避免 LLM 让人重做已经 auto discovery 拒绝过的同 recipe。
        """
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                "SELECT factor_name FROM factor_proposals "
                "WHERE recipe_json = ? AND recipe_kind = 'dsl' LIMIT 1",
                (recipe_json,),
            ).fetchone()
        return row[0] if row else None

    def exists_code_hash(self, code_hash: str) -> str | None:
        """按 code_hash 查重 (code 路径)。LLM 跨 session 提议同源代码时直接 skip。"""
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                "SELECT factor_name FROM factor_proposals "
                "WHERE code_hash = ? AND recipe_kind = 'code' LIMIT 1",
                (code_hash,),
            ).fetchone()
        return row[0] if row else None

    def upsert(self, proposal: FactorProposal) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                INSERT INTO factor_proposals
                  (factor_name, recipe_kind, recipe_json, direction, status,
                   ic_mean, ic_std, ir, t_stat, max_abs_corr, reason,
                   created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir,
                   recipe_code, code_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name) DO UPDATE SET
                  recipe_kind=excluded.recipe_kind,
                  -- promote 时 OOS IR < 0 会 flip direction 并改写 recipe_json;
                  -- 之前 update set 里漏了这两个字段, 落库丢失, daemon 重启 restore 会读老 recipe.
                  recipe_json=excluded.recipe_json,
                  direction=excluded.direction,
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
                  oos_ir=excluded.oos_ir,
                  recipe_code=excluded.recipe_code,
                  code_hash=excluded.code_hash
                """,
                (
                    proposal.factor_name,
                    proposal.recipe_kind,
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
                    proposal.recipe_code,
                    proposal.code_hash,
                ),
            )
            conn.commit()

    # 重构: 集中所有 SELECT 的列名顺序, upsert 和 list_* 共用
    _SELECT_COLS = (
        "factor_name, recipe_kind, recipe_json, direction, status, "
        "ic_mean, ic_std, ir, t_stat, max_abs_corr, reason, "
        "created_at, evaluated_at, shadow_started_at, oos_observations, oos_ir, "
        "recipe_code, code_hash"
    )

    def list_accepted(self) -> list[FactorProposal]:
        """已晋升 / shadow 的因子（status in (accepted, shadow)）—— 都进内存 registry。

        M19 review P0-2: 过滤 evicted_at IS NOT NULL — 被淘汰的不进 registry。
        """
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                "WHERE status IN ('accepted', 'shadow') "
                "  AND evicted_at IS NULL "
                "ORDER BY evaluated_at DESC"
            ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def list_shadow(self) -> list[FactorProposal]:
        """正在 OOS 观察的 shadow 因子（每轮 discovery 复评、N 天后 promote）。

        M19 review P0-2: 过滤 evicted — 被淘汰的不再参与 promote_shadows。
        """
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                "WHERE status = 'shadow' AND evicted_at IS NULL "
                "ORDER BY shadow_started_at ASC"
            ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def list_recent(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        recipe_kind: str | None = None,
    ) -> list[FactorProposal]:
        """重构: 支持 recipe_kind 过滤 (dsl / code / None=全部)。"""
        with open_meta_db(self._db) as conn:
            if status is None and recipe_kind is None:
                rows = conn.execute(
                    f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                    "WHERE evicted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif status is not None and recipe_kind is None:
                rows = conn.execute(
                    f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                    "WHERE status = ? AND evicted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            elif status is None and recipe_kind is not None:
                rows = conn.execute(
                    f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                    "WHERE recipe_kind = ? AND evicted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (recipe_kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {self._SELECT_COLS} FROM factor_proposals "
                    "WHERE status = ? AND recipe_kind = ? AND evicted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, recipe_kind, limit),
                ).fetchall()
        return [FactorProposal(*r) for r in rows]

    def list_existing_recipes(self, *, limit: int = 400) -> list[str]:
        """M24 brainstorm: 列出库里所有已尝试过的 recipe_json (去重, 按最近插入倒序)。

        用途：塞进 LLM prompt，让 LLM 看到要避开的具体组合，避免它随机提议全部命中重复。
        候选空间只有 5×8×5×2=400，理论上限就是 400。
        """
        seen: set[str] = set()
        out: list[str] = []
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT recipe_json FROM factor_proposals "
                "WHERE evicted_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        for (rj,) in rows:
            if rj in seen:
                continue
            seen.add(rj)
            out.append(rj)
            if len(out) >= limit:
                break
        return out

    def counts(self) -> dict[str, int]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM factor_proposals "
                "WHERE evicted_at IS NULL GROUP BY status"
            ).fetchall()
        return {status: count for status, count in rows}


def now_iso() -> str:
    # 用本地时间（系统 timezone = Asia/Shanghai），跟 daemon log / web UI 一致。
    # 之前用 utcnow() 导致 evaluated_at 显示比本地时间晚 8 小时（北京时间 14:00 显示 06:00）。
    return datetime.now().isoformat(timespec="seconds")


def recipe_to_json(recipe: dict) -> str:
    return json.dumps(recipe, sort_keys=True, ensure_ascii=False)


def recipe_from_json(s: str) -> dict:
    return json.loads(s)
