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
# 任何新 kind 必须在这里登记，否则 _validate_kind 会发 warning。
KNOWN_EVENT_KINDS: frozenset[str] = frozenset(
    {
        # P2 自身
        "batch.post_close.completed",
        "batch.post_close.failed",
        "batch.post_close.skipped",
        "batch.post_close.timeout",
        "batch.post_close.crashed",
        "batch.post_close.interrupted",
        "batch.deep_research.completed",
        "batch.deep_research.failed",
        "batch.deep_research.skipped",
        "batch.deep_research.timeout",
        "batch.deep_research.crashed",
        "batch.deep_research.interrupted",
        "retry.fetch_errors.completed",
        "retry.fetch_errors.failed",
        "data.refresh.completed",
        "data.refresh.failed",
        "daemon.started",
        "daemon.stopped",
        # P3
        "factor.metric.deactivated",
        "factor.metric.activated",
        "factor.metric.bootstrap",
        "factor.metric.evaluated",
        "factor.data.missing",
        "portfolio.snapshot.generated",
        "portfolio.optimizer.fallback",
        # P4
        "analyst.brief.generated",
        "analyst.brief.degraded",
        "chat.session.created",
        "llm.tool.failed",
        "llm.tool.unknown",
    }
)


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
        if kind not in KNOWN_EVENT_KINDS:
            logger.warning("events.kind %r not in P2 附录 C registry; writing anyway", kind)

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
        """retention 清理：删除超龄的 events 和 job_runs（不删 running）。返回删除统计。"""
        ev_cutoff = (datetime.now() - timedelta(days=events_keep_days)).isoformat()
        jr_cutoff = (datetime.now() - timedelta(days=job_runs_keep_days)).isoformat()
        with open_meta_db(self._meta_db_path) as conn:
            ev_cursor = conn.execute("DELETE FROM events WHERE ts < ?", (ev_cutoff,))
            ev_deleted = ev_cursor.rowcount
            jr_cursor = conn.execute(
                "DELETE FROM job_runs WHERE finished_at IS NOT NULL AND finished_at < ? AND status != 'running'",
                (jr_cutoff,),
            )
            jr_deleted = jr_cursor.rowcount
            conn.commit()
        return {"events_deleted": ev_deleted, "job_runs_deleted": jr_deleted}
