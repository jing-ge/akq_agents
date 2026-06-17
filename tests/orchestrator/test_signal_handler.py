"""self_heal_on_boot + GracefulShutdown + mark_daemon_started/stopped 单元测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
from akq_agents.orchestrator.signal_handler import (
    GracefulShutdown,
    mark_daemon_started,
    mark_daemon_stopped,
    self_heal_on_boot,
)
from akq_agents.orchestrator.state_store import SchedulerStateStore


@pytest.fixture
def store(tmp_path: Path) -> SchedulerStateStore:
    return SchedulerStateStore(tmp_path / "meta.db")


@pytest.fixture
def daemon_state_file(tmp_path: Path) -> DaemonStateFile:
    return DaemonStateFile(tmp_path / "daemon_state.json")


def _old_ts(hours: int = 24) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat()


def test_self_heal_marks_old_running_as_crashed(store: SchedulerStateStore) -> None:
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-15",
        status="running",
        started_at=_old_ts(24),
    )
    stats = self_heal_on_boot(
        store=store, is_trading_day=lambda _d: False, older_than_hours=6
    )
    assert stats["crashed_marked"] == 1

    run = store.get_job_run("batch.post_close", "2026-06-15")
    assert run is not None and run.status == "crashed"

    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.crashed" for e in events)


def test_self_heal_also_marks_interrupted_as_crashed(store: SchedulerStateStore) -> None:
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-14",
        status="interrupted",
        started_at=_old_ts(48),
    )
    stats = self_heal_on_boot(
        store=store, is_trading_day=lambda _d: False, older_than_hours=6
    )
    assert stats["crashed_marked"] == 1


def test_self_heal_recent_running_not_touched(store: SchedulerStateStore) -> None:
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-17",
        status="running",
        started_at=(datetime.now() - timedelta(hours=1)).isoformat(),
    )
    stats = self_heal_on_boot(
        store=store, is_trading_day=lambda _d: True, older_than_hours=6
    )
    assert stats["crashed_marked"] == 0

    run = store.get_job_run("batch.post_close", "2026-06-17")
    assert run is not None and run.status == "running"


def test_self_heal_backfills_when_post_close_passed_and_no_ok(
    store: SchedulerStateStore,
) -> None:
    called = {"v": 0}

    def backfill():
        called["v"] += 1

    # post_close_hour 设为 0 让"当前已经过了"判定永远为 true（无论现在时刻）
    stats = self_heal_on_boot(
        store=store,
        is_trading_day=lambda _d: True,
        older_than_hours=6,
        post_close_hour=0,
        post_close_minute=0,
        backfill_post_close=backfill,
    )
    assert called["v"] == 1
    assert stats["backfilled"] == 1


def test_self_heal_does_not_backfill_when_already_ok(
    store: SchedulerStateStore,
) -> None:
    from datetime import date as _date

    today = _date.today().isoformat()
    store.upsert_job_run(
        job_id="batch.post_close",
        partition=today,
        status="ok",
        started_at=datetime.now().isoformat(),
        finished_at=datetime.now().isoformat(),
    )

    called = {"v": 0}
    self_heal_on_boot(
        store=store,
        is_trading_day=lambda _d: True,
        post_close_hour=0,
        post_close_minute=0,
        backfill_post_close=lambda: called.__setitem__("v", called["v"] + 1),
    )
    assert called["v"] == 0


def test_self_heal_does_not_backfill_on_non_trading_day(
    store: SchedulerStateStore,
) -> None:
    called = {"v": 0}
    self_heal_on_boot(
        store=store,
        is_trading_day=lambda _d: False,
        post_close_hour=0,
        post_close_minute=0,
        backfill_post_close=lambda: called.__setitem__("v", called["v"] + 1),
    )
    assert called["v"] == 0


def test_mark_daemon_started_writes_state_file(daemon_state_file: DaemonStateFile) -> None:
    mark_daemon_started(daemon_state_file=daemon_state_file, pid=999, version="test-1.0")
    state = daemon_state_file.read()
    assert state is not None
    assert state.status == "running"
    assert state.pid == 999
    assert state.version == "test-1.0"


def test_mark_daemon_stopped_preserves_other_fields(daemon_state_file: DaemonStateFile) -> None:
    mark_daemon_started(daemon_state_file=daemon_state_file, pid=999, version="x")
    mark_daemon_stopped(daemon_state_file=daemon_state_file)
    state = daemon_state_file.read()
    assert state is not None
    assert state.status == "stopped"
    assert state.pid == 999


def test_graceful_shutdown_request_stop_sets_event() -> None:
    gs = GracefulShutdown()
    assert not gs.should_stop
    gs.request_stop()
    assert gs.should_stop


def test_graceful_shutdown_wait_unblocks_after_request_stop() -> None:
    gs = GracefulShutdown()
    done = threading.Event()

    def waiter() -> None:
        gs.wait(timeout=5.0)
        done.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    gs.request_stop()
    assert done.wait(timeout=2.0)
