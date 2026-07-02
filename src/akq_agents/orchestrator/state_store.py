"""P2 SchedulerStateStore + events.kind enum + Event/JobRun dataclasses。

负责 :class:`SchedulerStateStore`：``job_runs`` + ``events`` 两张表的读写封装。
单一职责：不知道业务，只管"按 (job_id, partition) 幂等记账 + 写事件"。

events 写入失败 → fallback 到 stderr 日志，不抛异常，不影响 job 主流程
（spec §4 关键边界）。

events.kind 枚举见 :data:`KNOWN_EVENT_KINDS`，命名规范见 P2 附录 C：
``<domain>.<noun>.<verb_past>``。未知 kind 仍允许写入但会发 warning（保留前向兼容）。
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


# P2 附录 C：events.kind 命名规范注册表
# 任何新 kind 必须以下面某个前缀开头，否则 _validate_kind 会发 warning。
# 用前缀而非精确匹配，避免每加一个 {job_id}.crashed/.failed/... 都要改这里。
KNOWN_EVENT_KIND_PREFIXES: tuple[str, ...] = (
    # P2 调度系统
    "batch.",            # batch.post_close.* / batch.deep_research.*
    "retry.",            # retry.fetch_errors.*
    "data.",             # data.refresh.* / data.refresh_daily.*
    "daemon.",           # daemon.started / daemon.stopped
    "health.",           # health.heartbeat.*
    # P3 factor pipeline
    "factor.",           # factor.metric.* / factor.discovery.* / factor.brainstorm.* / factor.promote_shadows_failed
    "portfolio.",        # portfolio.snapshot.* / portfolio.optimizer.* / portfolio.backtester.*
    "trade_list.",       # trade_list.generation_failed
    "paper_trading.",    # paper_trading.update_failed
    # P4 LLM / chat
    "analyst.",          # analyst.brief.*
    "chat.",             # chat.session.*
    "llm.",              # llm.tool.*
    # M17 alerter
    "alert.",            # alert.check.* / alert.nav.* / alert.data.* / alert.factor.*
)


def _kind_is_known(kind: str) -> bool:
    return any(kind.startswith(p) for p in KNOWN_EVENT_KIND_PREFIXES)


# 兼容旧 import（如有外部代码 import KNOWN_EVENT_KINDS）— 改为基于前缀的虚拟集合
# 实际校验走 _kind_is_known
KNOWN_EVENT_KINDS: frozenset[str] = frozenset()  # deprecated; 用 KNOWN_EVENT_KIND_PREFIXES


_JOB_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  partition TEXT NOT NULL,
  status TEXT NOT NULL,
  reason_code TEXT,
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  payload_json TEXT,
  UNIQUE(job_id, partition)
);
"""

_JOB_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_job_runs_status_started ON job_runs(status, started_at);
"""

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  source TEXT,
  payload_json TEXT
);
"""

_EVENTS_TS_INDEX = "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);"
_EVENTS_KIND_TS_INDEX = "CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);"


# M23: web → daemon 手动触发通道. web 永远不跑重 CPU 业务 (pandas/numpy 8 worker 池
# 5s 内把 web event loop 饿死, data-freshness 端点超时). web 端点 POST /jobs/{name}/trigger
# 改为: 写一行 pending_triggers + 写 job_runs.status='pending' + 立刻返回 202.
# daemon 周期任务 manual_trigger_picker 每 5s 扫一次, claim 后用 daemon 自己的 JobRunner
# 跑 (在 daemon 进程里, 8 worker 池只抢 daemon 进程的 CPU, 不影响 web).
#
# 设计原则:
# - claim 用 `UPDATE claimed_at = ? WHERE id = ? AND claimed_at IS NULL` 单语句原子操作,
#   多 daemon / 重启 daemon 都不会重复触发同一行.
# - payload_json 存 trigger 参数 (force_full, n_candidates, dry_run) — 未来扩展新参数
#   不用改 schema.
# - status 字段: pending → claimed → running/ok/failed (claimed 是 picker 拿走但还没调
#   JobRunner.run 的中间态; 写 job_runs.status 才是真正的 running).
# - 失败行不清, 留作 audit; retention 通过 cleanup() 周期删老 pending 行.
_PENDING_TRIGGERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  partition TEXT NOT NULL,
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TEXT NOT NULL,
  claimed_at TEXT,
  claimed_by TEXT,
  finished_at TEXT
);
"""
_PENDING_TRIGGERS_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pending_triggers_status_req "
    "ON pending_triggers(status, requested_at);"
)


# M24: web → daemon 异步计算结果存储. picker 跑完业务后, 把"前端要用的 payload"
# 单独存一行 job_results (key = (job_id, partition)). 跟 job_runs.payload_json 区分:
# - job_runs.payload_json: 业务自填的 metadata (n_items / duration / status), 给 ops 看板;
# - job_results.result_json: 前端要的"数据本身" (backtest NAV 数组 / trade_list items /
#   brainstorm suggestions), 经常是几百 KB, 单独存, 不挤 job_runs 的 64KB SQLITE_LIMIT_LENGTH.
#
# 设计:
# - (job_id, partition) UNIQUE: 一个 trigger 只产一个 result; 用户多次手动 trigger 用
#   manual-xxxxxx 不同 partition 互不覆盖.
# - result_json 留 NULL 直到 picker 写 result 时. web GET /jobs/{name}/{partition}/result
#   端点看 NULL 返 {"status": "running"} 让前端继续 poll; 写好返 result.
# - 不删老行 — cleanup 周期 delete finished_at < 7d 的行 (跟 pending_triggers 同步),
#   这样前端晚几天再开页面看历史 trigger 仍能找到.
_JOB_RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_results (
  job_id TEXT NOT NULL,
  partition TEXT NOT NULL,
  result_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (job_id, partition)
);
"""


JobStatus = str  # pending | running | ok | failed | skipped | timeout | crashed | interrupted
EventLevel = str  # info | warning | error


@dataclass
class JobRun:
    id: int
    job_id: str
    partition: str
    status: JobStatus
    reason_code: str | None
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    payload_json: str | None


@dataclass
class Event:
    id: int
    ts: str
    level: EventLevel
    kind: str
    source: str | None
    payload_json: str | None


class SchedulerStateStore:
    """``job_runs`` + ``events`` 表读写。复用 P1 ``meta.db`` 与 WAL 契约。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._meta_db_path = Path(meta_db_path)
        self._ensure_schema()

    # ---- schema ----

    def _ensure_schema(self) -> None:
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(_JOB_RUNS_SCHEMA)
            conn.execute(_JOB_RUNS_INDEX)
            conn.execute(_EVENTS_SCHEMA)
            conn.execute(_EVENTS_TS_INDEX)
            conn.execute(_EVENTS_KIND_TS_INDEX)
            conn.execute(_PENDING_TRIGGERS_SCHEMA)
            conn.execute(_PENDING_TRIGGERS_STATUS_INDEX)
            conn.execute(_JOB_RESULTS_SCHEMA)
            conn.commit()

    # ---- job_runs ----

    def upsert_job_run(
        self,
        *,
        job_id: str,
        partition: str,
        status: JobStatus,
        reason_code: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """对 ``(job_id, partition)`` upsert 一条 job_runs；多次调用幂等。"""
        payload_json = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                """
                INSERT INTO job_runs (job_id, partition, status, reason_code, started_at, finished_at, duration_ms, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, partition) DO UPDATE SET
                    status=excluded.status,
                    reason_code=excluded.reason_code,
                    started_at=COALESCE(excluded.started_at, job_runs.started_at),
                    finished_at=excluded.finished_at,
                    duration_ms=excluded.duration_ms,
                    payload_json=excluded.payload_json
                """,
                (
                    job_id,
                    partition,
                    status,
                    reason_code,
                    started_at,
                    finished_at,
                    duration_ms,
                    payload_json,
                ),
            )
            conn.commit()

    def get_job_run(self, job_id: str, partition: str) -> JobRun | None:
        with open_meta_db(self._meta_db_path) as conn:
            row = conn.execute(
                """
                SELECT id, job_id, partition, status, reason_code, started_at, finished_at, duration_ms, payload_json
                FROM job_runs WHERE job_id = ? AND partition = ?
                """,
                (job_id, partition),
            ).fetchone()
        return None if row is None else JobRun(*row)

    def list_recent_runs(
        self, *, limit: int = 50, job_id: str | None = None, status: str | None = None
    ) -> list[JobRun]:
        sql = (
            "SELECT id, job_id, partition, status, reason_code, started_at, finished_at, duration_ms, payload_json "
            "FROM job_runs WHERE 1=1"
        )
        params: list[Any] = []
        if job_id is not None:
            sql += " AND job_id = ?"
            params.append(job_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with open_meta_db(self._meta_db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [JobRun(*row) for row in rows]

    def list_runs_to_self_heal(self, *, older_than_hours: int = 6) -> list[JobRun]:
        """启动期 self_heal 扫描：找 status IN ('running','interrupted') 且 started_at 在 N 小时前的记录。"""
        cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, partition, status, reason_code, started_at, finished_at, duration_ms, payload_json
                FROM job_runs
                WHERE status IN ('running', 'interrupted')
                  AND (started_at IS NULL OR started_at < ?)
                """,
                (cutoff,),
            ).fetchall()
        return [JobRun(*row) for row in rows]

    def reclaim_stale_pending_triggers(self, *, older_than_minutes: int = 0) -> int:
        """启动期回收僵尸触发器: 把 status='claimed' 且未完成的记录标为 failed.

        场景: picker(daemon 进程)claim 了一条 trigger 后进程崩溃/被 kill, 该记录
        永远停在 'claimed', 于是 has_pending_or_running_for_job 一直判有活儿, 手动
        再触发同一 job 会被 409 拒绝。daemon 重启即意味着旧 picker 已死, 因此启动时
        把这些 claimed 记录一律回收。older_than_minutes>0 时只回收更早的记录。

        返回回收条数。
        """
        now = datetime.now()
        cutoff = (now - timedelta(minutes=older_than_minutes)).isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE pending_triggers
                SET status='failed', finished_at=?
                WHERE status='claimed'
                  AND finished_at IS NULL
                  AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (now.isoformat(), cutoff),
            )
            conn.commit()
            return cursor.rowcount

    def mark_crashed(self, job_run_id: int) -> None:
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                "UPDATE job_runs SET status='crashed', finished_at=? WHERE id=?",
                (datetime.now().isoformat(), job_run_id),
            )
            conn.commit()

    def mark_interrupted_running(self) -> int:
        """优雅停机时把 status='running' 转 'interrupted'，返回受影响行数。"""
        ts = datetime.now().isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            cursor = conn.execute(
                "UPDATE job_runs SET status='interrupted', finished_at=? WHERE status='running'",
                (ts,),
            )
            conn.commit()
            return cursor.rowcount

    # ---- events ----

    def write_event(
        self,
        *,
        level: EventLevel,
        kind: str,
        source: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """写一行 events；写入失败 fallback 到 stderr，不抛异常。"""
        self._validate_kind(kind)
        payload_json = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            with open_meta_db(self._meta_db_path) as conn:
                conn.execute(
                    "INSERT INTO events (ts, level, kind, source, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), level, kind, source, payload_json),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — events 写入失败必须不影响 job 主流程
            print(
                f"[events.write_event fallback] level={level} kind={kind} source={source} "
                f"payload={payload_json} error={exc!r}",
                file=sys.stderr,
            )

    def _validate_kind(self, kind: str) -> None:
        if not _kind_is_known(kind):
            logger.warning("events.kind %r not in P2 附录 C registry prefixes; writing anyway", kind)

    def list_events(
        self,
        *,
        limit: int = 50,
        level_min: str | None = None,
        kind_prefix: str | None = None,
        since: str | None = None,
    ) -> list[Event]:
        """列出最近 events。level_min: info|warning|error。"""
        sql = "SELECT id, ts, level, kind, source, payload_json FROM events WHERE 1=1"
        params: list[Any] = []
        if level_min == "warning":
            sql += " AND level IN ('warning', 'error')"
        elif level_min == "error":
            sql += " AND level = 'error'"
        if kind_prefix is not None:
            sql += " AND kind LIKE ?"
            params.append(kind_prefix + "%")
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with open_meta_db(self._meta_db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Event(*row) for row in rows]

    def events_count_24h_by_level(self) -> dict[str, int]:
        """24 小时内 events 按 level 分组计数（P5 ops 页用）。"""
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            rows = conn.execute(
                "SELECT level, COUNT(*) FROM events WHERE ts >= ? GROUP BY level",
                (cutoff,),
            ).fetchall()
        result = {"info": 0, "warning": 0, "error": 0}
        for level, count in rows:
            result[str(level)] = int(count)
        return result

    # ---- retention 清理 ----

    def cleanup(self, *, events_keep_days: int, job_runs_keep_days: int) -> dict[str, int]:
        """retention 清理：删除超龄的 events / job_runs / pending_triggers（不删 running）。返回删除统计。"""
        ev_cutoff = (datetime.now() - timedelta(days=events_keep_days)).isoformat()
        jr_cutoff = (datetime.now() - timedelta(days=job_runs_keep_days)).isoformat()
        # pending_triggers 保留 7 天（job_runs_days / 2 兜底），删已经 finish 的行（无论 ok/failed），
        # 留 pending / claimed 行做 audit（picker crash 时可重试或人工 cleanup）。
        pt_keep_days = max(1, job_runs_keep_days // 2)
        pt_cutoff = (datetime.now() - timedelta(days=pt_keep_days)).isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            ev_cursor = conn.execute("DELETE FROM events WHERE ts < ?", (ev_cutoff,))
            ev_deleted = ev_cursor.rowcount
            jr_cursor = conn.execute(
                "DELETE FROM job_runs WHERE finished_at IS NOT NULL AND finished_at < ? AND status != 'running'",
                (jr_cutoff,),
            )
            jr_deleted = jr_cursor.rowcount
            pt_cursor = conn.execute(
                "DELETE FROM pending_triggers WHERE requested_at < ? AND status IN ('ok', 'failed')",
                (pt_cutoff,),
            )
            pt_deleted = pt_cursor.rowcount
            conn.commit()
        return {
            "events_deleted": ev_deleted,
            "job_runs_deleted": jr_deleted,
            "pending_triggers_deleted": pt_deleted,
        }

    # ---- pending_triggers (M23: web → daemon 手动触发通道) ----

    def create_pending_trigger(
        self,
        *,
        job_id: str,
        partition: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """web 写一行 pending trigger, 返回 row id。payload (force_full/n_candidates 等)
        序列化到 payload_json 备 picker 读取。"""
        payload_json = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with open_meta_db(self._meta_db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO pending_triggers (job_id, partition, payload_json, status, requested_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (job_id, partition, payload_json, datetime.now().isoformat()),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def claim_one_pending_trigger(self, *, claimed_by: str) -> dict[str, Any] | None:
        """daemon picker 原子拿一行: UPDATE ... WHERE claimed_at IS NULL; 没行返回 None.

        用单条 SQL 的 RETURNING (SQLite 3.35+) 拿行; 若环境 SQLite 太老就 SELECT + UPDATE 两步.
        web/daemon 共用 meta.db, claim 是单条 SQL 原子操作, 多进程/重启都不会重复.
        """
        with open_meta_db(self._meta_db_path) as conn:
            row = conn.execute(
                """
                UPDATE pending_triggers
                SET status='claimed', claimed_at=?, claimed_by=?
                WHERE id = (
                    SELECT id FROM pending_triggers
                    WHERE status='pending'
                    ORDER BY requested_at ASC
                    LIMIT 1
                )
                AND status='pending'
                RETURNING id, job_id, partition, payload_json, requested_at
                """,
                (datetime.now().isoformat(), claimed_by),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "job_id": str(row[1]),
            "partition": str(row[2]),
            "payload": json.loads(row[3]) if row[3] else {},
            "requested_at": str(row[4]),
        }

    def mark_trigger_finished(self, trigger_id: int, *, status: str) -> None:
        """picker 跑完后写 finished status (ok/failed). 留作 audit, 不删."""
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                "UPDATE pending_triggers SET status=?, finished_at=? WHERE id=?",
                (status, datetime.now().isoformat(), trigger_id),
            )
            conn.commit()

    def has_pending_or_running_for_job(self, job_id: str) -> bool:
        """并发防护: trigger 时检查, 同 job 已有 pending/claimed/running 任务则拒绝.

        实现: job_runs.status='running' + pending_triggers.status IN ('pending','claimed')
        任一存在即视为有活儿. 用一条 SQL 一次查, 避免 web 端和 daemon picker 双重判断的不一致.
        """
        with open_meta_db(self._meta_db_path) as conn:
            r1 = conn.execute(
                "SELECT 1 FROM job_runs WHERE job_id=? AND status='running' LIMIT 1",
                (job_id,),
            ).fetchone()
            if r1 is not None:
                return True
            r2 = conn.execute(
                "SELECT 1 FROM pending_triggers WHERE job_id=? AND status IN ('pending','claimed') LIMIT 1",
                (job_id,),
            ).fetchone()
            return r2 is not None

    # ---- job_results (M24: web → daemon 异步 result 存储) ----

    def get_job_result(self, job_id: str, partition: str) -> dict[str, Any] | None:
        """读 picker 写好的 result. 没行/result_json 仍 NULL 都返 None; web 端点转 {"status": "running"}.

        - None 含义: (1) 没 trigger 过 (web 端点返 404); (2) trigger 了但 picker 还没写 result (running).
        - 用 result_json IS NULL 区分"trigger 中"和"跑完失败" — 失败时 picker 写 {"error": "..."}.
        """
        with open_meta_db(self._meta_db_path) as conn:
            row = conn.execute(
                "SELECT result_json FROM job_results WHERE job_id=? AND partition=?",
                (job_id, partition),
            ).fetchone()
        if row is None:
            return None
        raw = row[0]
        if raw is None:
            return None  # trigger 中, picker 还没写
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"_decode_error": True, "raw_size": len(raw) if isinstance(raw, str) else 0}

    def set_job_result(self, job_id: str, partition: str, result: dict[str, Any]) -> None:
        """picker 业务跑完后写 result. 幂等 — 同一 (job_id, partition) 覆盖写.

        大小写: SQLite 单 cell 默认上限 1GB; 我们的 backtest NAV + 因子回测 < 1MB,
        brainstorm 建议 20 条 < 10KB, 都在安全区. 写不进时 SQLite 抛 DatabaseError
        (不静默吞), 由 picker 异常路径标 failed.
        """
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                """
                INSERT INTO job_results (job_id, partition, result_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, partition) DO UPDATE SET
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (job_id, partition, result_json, datetime.now().isoformat()),
            )
            conn.commit()
