"""DaemonStateFile 单元测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from akq_agents.orchestrator.daemon_state_file import DaemonState, DaemonStateFile


@pytest.fixture
def state_file(tmp_path: Path) -> DaemonStateFile:
    return DaemonStateFile(tmp_path / "daemon_state.json")


def _make_state(**overrides) -> DaemonState:
    defaults = {
        "status": "running",
        "pid": 12345,
        "started_at": datetime.now().isoformat(),
        "last_heartbeat": datetime.now().isoformat(),
        "version": "akq-agents 0.2.0",
    }
    defaults.update(overrides)
    return DaemonState(**defaults)


def test_write_and_read_roundtrip(state_file: DaemonStateFile) -> None:
    state = _make_state()
    state_file.write(state)

    read = state_file.read()
    assert read is not None
    assert read.status == "running"
    assert read.pid == 12345


def test_read_returns_none_when_file_missing(tmp_path: Path) -> None:
    sf = DaemonStateFile(tmp_path / "absent.json")
    assert sf.read() is None


def test_read_returns_none_on_corrupted_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert DaemonStateFile(p).read() is None


def test_update_heartbeat_preserves_other_fields(state_file: DaemonStateFile) -> None:
    original = _make_state(last_heartbeat="2020-01-01T00:00:00")
    state_file.write(original)

    state_file.update_heartbeat()

    read = state_file.read()
    assert read is not None
    assert read.last_heartbeat != "2020-01-01T00:00:00"
    assert read.pid == original.pid
    assert read.status == "running"


def test_update_heartbeat_noop_when_no_file(state_file: DaemonStateFile) -> None:
    # 不应抛
    state_file.update_heartbeat()
    assert state_file.read() is None


def test_is_alive_true_when_recent_heartbeat(state_file: DaemonStateFile) -> None:
    state_file.write(_make_state(last_heartbeat=datetime.now().isoformat()))
    assert state_file.is_alive(max_age_s=600)


def test_is_alive_false_when_heartbeat_too_old(state_file: DaemonStateFile) -> None:
    old = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file.write(_make_state(last_heartbeat=old))
    assert not state_file.is_alive(max_age_s=600)


def test_is_alive_false_when_status_stopped(state_file: DaemonStateFile) -> None:
    state_file.write(_make_state(status="stopped"))
    assert not state_file.is_alive(max_age_s=600)


def test_is_alive_false_when_no_file(tmp_path: Path) -> None:
    sf = DaemonStateFile(tmp_path / "nope.json")
    assert not sf.is_alive()


def test_atomic_write_uses_rename(tmp_path: Path) -> None:
    """写入过程中不应出现半成品文件（验证：写完后 .tmp 不存在）。"""
    sf = DaemonStateFile(tmp_path / "d.json")
    sf.write(_make_state())
    assert (tmp_path / "d.json").exists()
    assert not (tmp_path / "d.json.tmp").exists()


def test_serialized_payload_contains_all_fields(tmp_path: Path) -> None:
    """与 P5 渲染契约对齐：JSON 包含 status/pid/started_at/last_heartbeat/version。"""
    sf = DaemonStateFile(tmp_path / "d.json")
    sf.write(_make_state(status="starting"))
    data = json.loads((tmp_path / "d.json").read_text())
    assert set(data) == {"status", "pid", "started_at", "last_heartbeat", "version"}
