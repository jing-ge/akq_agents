"""JobRunner 单元测试。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from akq_agents.orchestrator.job_runner import JobRunner
from akq_agents.orchestrator.state_store import SchedulerStateStore


@pytest.fixture
def store(tmp_path: Path) -> SchedulerStateStore:
    return SchedulerStateStore(tmp_path / "meta.db")


@pytest.fixture
def runner(store: SchedulerStateStore) -> JobRunner:
    # 测试用单线程 executor 便于断言
    return JobRunner(
        store,
        is_trading_day=lambda _d: True,
        executor=ThreadPoolExecutor(max_workers=2),
    )


def test_run_success_writes_ok_and_event(runner: JobRunner, store: SchedulerStateStore) -> None:
    result = runner.run(
        "batch.post_close",
        "2026-06-17",
        lambda: {"n": 5},
        timeout_s=10,
    )
    assert result.status == "ok"
    assert result.duration_ms is not None and result.duration_ms >= 0

    run = store.get_job_run("batch.post_close", "2026-06-17")
    assert run is not None
    assert run.status == "ok"
    assert json.loads(run.payload_json or "{}") == {"n": 5}

    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.completed" for e in events)


def test_run_idempotent_when_already_ok(runner: JobRunner, store: SchedulerStateStore) -> None:
    runner.run("batch.post_close", "2026-06-17", lambda: {"n": 1}, timeout_s=10)
    call_count = {"v": 0}

    def fn():
        call_count["v"] += 1
        return {"n": 99}

    result = runner.run("batch.post_close", "2026-06-17", fn, timeout_s=10)
    assert result.status == "noop"
    assert result.reason_code == "ALREADY_OK"
    assert call_count["v"] == 0  # fn 没被调用


def test_run_trading_day_guard_skips_non_trading_day(store: SchedulerStateStore) -> None:
    runner = JobRunner(
        store,
        is_trading_day=lambda _d: False,
        executor=ThreadPoolExecutor(max_workers=1),
    )
    called = {"v": False}

    def fn():
        called["v"] = True
        return {}

    result = runner.run("batch.post_close", "2026-06-14", fn, timeout_s=10)
    assert result.status == "skipped"
    assert result.reason_code == "NOT_TRADING_DAY"
    assert called["v"] is False

    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.skipped" for e in events)


def test_run_trading_day_guard_only_for_whitelist(store: SchedulerStateStore) -> None:
    """retry.fetch_errors 不在白名单 → 非交易日也跑。"""
    runner = JobRunner(
        store,
        is_trading_day=lambda _d: False,
        executor=ThreadPoolExecutor(max_workers=1),
    )
    result = runner.run("retry.fetch_errors", "2026-06-14T10:00", lambda: {"resolved": 3}, timeout_s=10)
    assert result.status == "ok"


def test_run_timeout_records_timeout_status(store: SchedulerStateStore) -> None:
    runner = JobRunner(
        store,
        is_trading_day=lambda _d: True,
        executor=ThreadPoolExecutor(max_workers=1),
    )

    def slow_fn():
        time.sleep(2)
        return {}

    # timeout_s=0 → 立刻超时
    result = runner.run("batch.post_close", "2026-06-17", slow_fn, timeout_s=0)
    assert result.status == "timeout"
    assert result.reason_code == "TIMEOUT"

    run = store.get_job_run("batch.post_close", "2026-06-17")
    assert run is not None
    assert run.status == "timeout"

    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.timeout" and e.level == "warning" for e in events)


def test_run_exception_records_failed(runner: JobRunner, store: SchedulerStateStore) -> None:
    def boom():
        raise RuntimeError("kaboom")

    result = runner.run("batch.post_close", "2026-06-17", boom, timeout_s=10)
    assert result.status == "failed"
    assert result.reason_code == "UNKNOWN"

    run = store.get_job_run("batch.post_close", "2026-06-17")
    assert run is not None
    assert run.status == "failed"
    assert "kaboom" in (run.payload_json or "")

    events = store.list_events(limit=5)
    err_evt = next((e for e in events if e.kind == "batch.post_close.failed"), None)
    assert err_evt is not None
    assert err_evt.level == "error"
    assert "kaboom" in (err_evt.payload_json or "")


def test_run_data_not_ready_records_skipped(runner: JobRunner, store: SchedulerStateStore) -> None:
    """DataNotReady → 视为 skipped 而非 failed，并发 .skipped event（不是 .failed）。"""
    from akq_agents.services.data.exceptions import DataNotReady

    def not_ready():
        raise DataNotReady({"600519": [date(2026, 6, 17)]})

    result = runner.run("batch.post_close", "2026-06-17", not_ready, timeout_s=10)
    assert result.status == "skipped"
    assert result.reason_code == "DATA_NOT_READY"

    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.skipped" for e in events)


def test_guard_exception_records_failed(store: SchedulerStateStore) -> None:
    def boom_guard(_d):
        raise RuntimeError("calendar broken")

    runner = JobRunner(
        store,
        is_trading_day=boom_guard,
        executor=ThreadPoolExecutor(max_workers=1),
    )
    result = runner.run("batch.post_close", "2026-06-17", lambda: {}, timeout_s=10)
    assert result.status == "failed"
    assert result.reason_code == "GUARD_ERROR"


def test_payload_dict_recorded_in_event_summary(runner: JobRunner, store: SchedulerStateStore) -> None:
    runner.run(
        "batch.post_close",
        "2026-06-17",
        lambda: {"n": 50, "turnover": 0.18},
        timeout_s=10,
    )
    events = store.list_events(limit=5)
    completed = next(e for e in events if e.kind == "batch.post_close.completed")
    payload = json.loads(completed.payload_json or "{}")
    assert payload["partition"] == "2026-06-17"
    assert payload["n"] == 50
    assert payload["turnover"] == 0.18
