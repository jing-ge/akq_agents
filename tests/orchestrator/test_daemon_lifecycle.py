"""QuantDaemon 端到端集成测试。

测试 daemon 启停 + jobs 注册 + self_heal + heartbeat 全链路（不实际等 cron 触发）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from akq_agents.models.scheduler_config import (
    BatchJobConfig,
    IntervalJobConfig,
    SchedulerConfig,
    SchedulerJobsConfig,
)
from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
from akq_agents.orchestrator.scheduler import QuantDaemon
from akq_agents.orchestrator.state_store import SchedulerStateStore


def _make_cfg(*, batch_enabled: bool = True, retry_enabled: bool = False, heartbeat_enabled: bool = False) -> SchedulerConfig:
    return SchedulerConfig(
        timezone="UTC",
        thread_pool_size=2,
        shutdown_grace_s=2,
        jobs=SchedulerJobsConfig(
            batch_post_close=BatchJobConfig(enabled=batch_enabled, timeout_s=10, hour=15, minute=30),
            batch_deep_research=BatchJobConfig(enabled=False),
            retry_fetch_errors=IntervalJobConfig(enabled=retry_enabled, interval_minutes=5, timeout_s=10),
            health_heartbeat=IntervalJobConfig(enabled=heartbeat_enabled, interval_minutes=5, timeout_s=5),
        ),
    )


@pytest.fixture
def daemon_assets(tmp_path: Path) -> dict[str, Any]:
    store = SchedulerStateStore(tmp_path / "meta.db")
    state_file = DaemonStateFile(tmp_path / "daemon_state.json")
    workflow = MagicMock()
    workflow.run_once.return_value = {
        "advisor-agent": {"rendered": "ok"},
        "portfolio-agent": {"portfolio_size": 10},
    }
    services = {"workflow": workflow}
    return {
        "store": store,
        "state_file": state_file,
        "services": services,
        "workflow": workflow,
    }


def test_daemon_start_and_stop_lifecycle(daemon_assets: dict[str, Any], tmp_path: Path) -> None:
    cfg = _make_cfg()
    daemon = QuantDaemon(
        cfg,
        daemon_assets["services"],
        state_store=daemon_assets["store"],
        daemon_state_file=daemon_assets["state_file"],
        is_trading_day=lambda _d: True,
        install_signals=False,  # 测试中不装信号处理器
    )

    # 在子线程跑 daemon
    t = threading.Thread(target=daemon.start, kwargs={"block": True}, daemon=True)
    t.start()

    # 等启动完成
    for _ in range(30):
        state = daemon_assets["state_file"].read()
        if state is not None and state.status == "running":
            break
        time.sleep(0.1)
    state = daemon_assets["state_file"].read()
    assert state is not None
    assert state.status == "running"

    # daemon.started event 应已写入
    events = daemon_assets["store"].list_events(limit=10)
    assert any(e.kind == "daemon.started" for e in events)

    # 触发停机
    daemon.request_stop()
    t.join(timeout=10)
    assert not t.is_alive()

    # 停机后状态
    final_state = daemon_assets["state_file"].read()
    assert final_state is not None
    assert final_state.status == "stopped"

    events_after = daemon_assets["store"].list_events(limit=20)
    assert any(e.kind == "daemon.stopped" for e in events_after)


def test_self_heal_on_boot_marks_old_running_crashed(daemon_assets: dict[str, Any]) -> None:
    store: SchedulerStateStore = daemon_assets["store"]
    # 预置一个 24h 前的 running 记录
    store.upsert_job_run(
        job_id="batch.post_close",
        partition="2026-06-15",
        status="running",
        started_at=(datetime.now() - timedelta(hours=24)).isoformat(),
    )

    cfg = _make_cfg(batch_enabled=False)  # 不真的注册 cron job
    daemon = QuantDaemon(
        cfg,
        daemon_assets["services"],
        state_store=store,
        daemon_state_file=daemon_assets["state_file"],
        is_trading_day=lambda _d: True,
        install_signals=False,
    )

    t = threading.Thread(target=daemon.start, kwargs={"block": True}, daemon=True)
    t.start()
    for _ in range(30):
        state = daemon_assets["state_file"].read()
        if state is not None and state.status == "running":
            break
        time.sleep(0.1)

    # self_heal 应已把 running 转 crashed
    run = store.get_job_run("batch.post_close", "2026-06-15")
    assert run is not None
    assert run.status == "crashed"

    daemon.request_stop()
    t.join(timeout=5)


def test_daemon_status_payload(daemon_assets: dict[str, Any]) -> None:
    cfg = _make_cfg(batch_enabled=False)
    daemon = QuantDaemon(
        cfg,
        daemon_assets["services"],
        state_store=daemon_assets["store"],
        daemon_state_file=daemon_assets["state_file"],
        is_trading_day=lambda _d: True,
        install_signals=False,
    )
    t = threading.Thread(target=daemon.start, kwargs={"block": True}, daemon=True)
    t.start()
    for _ in range(30):
        if daemon_assets["state_file"].read() is not None:
            break
        time.sleep(0.1)

    payload = daemon.status_payload()
    assert payload["state"] is not None
    assert payload["state"]["status"] == "running"
    assert payload["is_alive"] is True

    daemon.request_stop()
    t.join(timeout=5)


def test_daemon_registers_jobs_when_enabled(daemon_assets: dict[str, Any]) -> None:
    cfg = _make_cfg(batch_enabled=True, retry_enabled=False, heartbeat_enabled=True)
    daemon_assets["services"]["retry_worker"] = MagicMock()  # 装上 retry_worker 以便测试 retry 注册

    daemon = QuantDaemon(
        cfg,
        daemon_assets["services"],
        state_store=daemon_assets["store"],
        daemon_state_file=daemon_assets["state_file"],
        is_trading_day=lambda _d: True,
        install_signals=False,
    )
    t = threading.Thread(target=daemon.start, kwargs={"block": True}, daemon=True)
    t.start()
    for _ in range(30):
        if daemon_assets["state_file"].read() is not None:
            break
        time.sleep(0.1)

    # 内部 scheduler 应已注册 batch.post_close + health.heartbeat（共 2）；retry 未 enable
    assert daemon._scheduler is not None
    job_ids = {j.id for j in daemon._scheduler.get_jobs()}
    assert "batch.post_close" in job_ids
    assert "health.heartbeat" in job_ids

    daemon.request_stop()
    t.join(timeout=5)


def test_interrupted_marker_on_shutdown(daemon_assets: dict[str, Any]) -> None:
    """优雅停机前如有 running job_runs，应被标记 interrupted。

    用一个非"今天"的 partition 避免被 self_heal_on_boot 当作"需补跑的 batch.post_close"。
    """
    store: SchedulerStateStore = daemon_assets["store"]
    # 用一个"非当天 + 不太久"的 partition，self_heal 不会扫到（< 6h）也不会补跑（!= today）
    recent_other_day = (datetime.now() - timedelta(hours=2)).isoformat()
    store.upsert_job_run(
        job_id="batch.deep_research",       # 用 deep_research 避免触发 post_close 补跑
        partition="2026-06-15",
        status="running",
        started_at=recent_other_day,
    )

    cfg = _make_cfg(batch_enabled=False)
    daemon = QuantDaemon(
        cfg,
        daemon_assets["services"],
        state_store=store,
        daemon_state_file=daemon_assets["state_file"],
        is_trading_day=lambda _d: True,
        install_signals=False,
    )
    t = threading.Thread(target=daemon.start, kwargs={"block": True}, daemon=True)
    t.start()
    for _ in range(30):
        if daemon_assets["state_file"].read() is not None:
            break
        time.sleep(0.1)

    daemon.request_stop()
    t.join(timeout=5)

    run = store.get_job_run("batch.deep_research", "2026-06-15")
    assert run is not None
    assert run.status == "interrupted"
