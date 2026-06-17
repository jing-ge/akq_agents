"""SchedulerStateStore 单元测试。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from akq_agents.orchestrator.state_store import (
    KNOWN_EVENT_KINDS,
    SchedulerStateStore,
)


@pytest.fixture
def store(tmp_path: Path) -> SchedulerStateStore:
    return SchedulerStateStore(tmp_path / "meta.db")


def test_schema_creates_tables(tmp_path: Path) -> None:
    SchedulerStateStore(tmp_path / "meta.db")
    with sqlite3.connect(tmp_path / "meta.db") as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"job_runs", "events"}.issubset(tables)


def test_wal_mode_inherited_from_p1_open_meta_db(tmp_path: Path) -> None:
    """SchedulerStateStore 复用 P1 open_meta_db，应继承 WAL。"""
    SchedulerStateStore(tmp_path / "meta.db")
    with sqlite3.connect(tmp_path / "meta.db") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_upsert_job_run_idempotent(store: SchedulerStateStore) -> None:
    store.upsert_job_run(job_id="batch.post_close", partition="2026-06-17", status="running")
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-17",
        status="ok",
        started_at="2026-06-17T15:30:00",
        finished_at="2026-06-17T15:35:00",
        duration_ms=300_000,
        payload={"n": 50},
    )
    run = store.get_job_run("batch.post_close", "2026-06-17")
    assert run is not None
    assert run.status == "ok"
    assert run.duration_ms == 300_000
    assert json.loads(run.payload_json or "{}") == {"n": 50}


def test_list_recent_runs_filter_and_order(store: SchedulerStateStore) -> None:
    for d in ["2026-06-10", "2026-06-11", "2026-06-12"]:
        store.upsert_job_run(job_id="batch.post_close", partition=d, status="ok")
    store.upsert_job_run(job_id="retry.fetch_errors", partition="2026-06-12T10", status="ok")

    rows = store.list_recent_runs(limit=10, job_id="batch.post_close")
    assert len(rows) == 3
    # DESC by id
    assert [r.partition for r in rows] == ["2026-06-12", "2026-06-11", "2026-06-10"]

    rows_all = store.list_recent_runs(limit=10)
    assert len(rows_all) == 4


def test_list_runs_to_self_heal(store: SchedulerStateStore) -> None:
    # 一条 5 小时前的 running（不被扫到）
    recent_ts = (datetime.now() - timedelta(hours=5)).isoformat()
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-16",
        status="running",
        started_at=recent_ts,
    )
    # 一条 24 小时前的 running（应被扫到）
    old_ts = (datetime.now() - timedelta(hours=24)).isoformat()
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-15",
        status="running",
        started_at=old_ts,
    )
    # 一条 interrupted（无 started_at，也应被扫到）
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-14",
        status="interrupted",
    )

    rows = store.list_runs_to_self_heal(older_than_hours=6)
    partitions = {r.partition for r in rows}
    assert "2026-06-15" in partitions
    assert "2026-06-14" in partitions
    assert "2026-06-16" not in partitions  # too recent


def test_mark_crashed_and_interrupted(store: SchedulerStateStore) -> None:
    store.upsert_job_run(job_id="batch.post_close", partition="p1", status="running")
    store.upsert_job_run(job_id="batch.post_close", partition="p2", status="running")

    affected = store.mark_interrupted_running()
    assert affected == 2

    runs = store.list_recent_runs(limit=10, status="interrupted")
    assert len(runs) == 2

    # crash 单条
    run = store.get_job_run("batch.post_close", "p1")
    assert run is not None
    store.mark_crashed(run.id)
    refreshed = store.get_job_run("batch.post_close", "p1")
    assert refreshed is not None and refreshed.status == "crashed"


def test_write_event_known_kind(store: SchedulerStateStore) -> None:
    store.write_event(
        level="info",
        kind="batch.post_close.completed",
        source="batch.post_close",
        payload={"duration_ms": 1234},
    )
    events = store.list_events(limit=5)
    assert len(events) == 1
    assert events[0].kind == "batch.post_close.completed"
    assert json.loads(events[0].payload_json or "{}") == {"duration_ms": 1234}


def test_write_event_unknown_kind_still_persists(store: SchedulerStateStore, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    caplog.set_level(logging.WARNING)
    store.write_event(level="info", kind="totally.unknown.event")
    events = store.list_events(limit=5)
    assert len(events) == 1
    assert any("totally.unknown.event" in m for m in caplog.messages)


def test_write_event_db_failure_falls_back_to_stderr(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """events 写入失败时 fallback 到 stderr，不抛异常。"""
    store = SchedulerStateStore(tmp_path / "meta.db")
    # 让 db 文件指向一个不存在的目录，破坏后续 connect
    store._meta_db_path = tmp_path / "nonexistent" / "meta.db"

    # 这一次写应失败但不抛
    store.write_event(level="error", kind="batch.post_close.failed", source="test")

    captured = capfd.readouterr()
    assert "events.write_event fallback" in captured.err
    assert "batch.post_close.failed" in captured.err


def test_events_count_24h_by_level(store: SchedulerStateStore) -> None:
    store.write_event(level="info", kind="batch.post_close.completed")
    store.write_event(level="info", kind="batch.post_close.completed")
    store.write_event(level="warning", kind="batch.post_close.timeout")
    counts = store.events_count_24h_by_level()
    assert counts["info"] == 2
    assert counts["warning"] == 1
    assert counts["error"] == 0


def test_list_events_filter_by_level_and_kind(store: SchedulerStateStore) -> None:
    store.write_event(level="info", kind="batch.post_close.completed")
    store.write_event(level="warning", kind="batch.post_close.timeout")
    store.write_event(level="error", kind="batch.post_close.failed")

    warn_plus = store.list_events(limit=10, level_min="warning")
    assert {e.level for e in warn_plus} == {"warning", "error"}

    batch_only = store.list_events(limit=10, kind_prefix="batch.post_close")
    assert len(batch_only) == 3


def test_cleanup_retention(store: SchedulerStateStore) -> None:
    # 老 events
    old_ts = (datetime.now() - timedelta(days=40)).isoformat()
    with sqlite3.connect(store._meta_db_path) as conn:
        conn.execute(
            "INSERT INTO events (ts, level, kind, source, payload_json) VALUES (?, 'info', 'batch.post_close.completed', 'test', NULL)",
            (old_ts,),
        )
        # 老 job_run
        conn.execute(
            "INSERT INTO job_runs (job_id, partition, status, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
            ("batch.post_close", "2025-01-01", "ok", old_ts, old_ts),
        )
        conn.commit()

    # 新数据
    store.write_event(level="info", kind="batch.post_close.completed")
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-17",
        status="ok",
        finished_at=datetime.now().isoformat(),
    )

    stats = store.cleanup(events_keep_days=30, job_runs_keep_days=30)
    assert stats["events_deleted"] == 1
    assert stats["job_runs_deleted"] == 1

    remaining_events = store.list_events(limit=10)
    assert len(remaining_events) == 1
    remaining_runs = store.list_recent_runs(limit=10)
    assert len(remaining_runs) == 1


def test_known_event_kinds_includes_all_needed() -> None:
    """跨 spec 一致性自检：P3/P4 用到的 kind 必须在 enum 里。"""
    must_have = {
        "batch.post_close.completed",
        "retry.fetch_errors.completed",
        "factor.metric.evaluated",
        "portfolio.snapshot.generated",
        "analyst.brief.generated",
        "analyst.brief.degraded",
        "chat.session.created",
        "llm.tool.failed",
        "llm.tool.unknown",
        "daemon.started",
        "daemon.stopped",
    }
    assert must_have.issubset(KNOWN_EVENT_KINDS)
