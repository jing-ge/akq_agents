"""``batch.post_close`` job 端到端集成测试（注入 mock workflow）。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner
from akq_agents.orchestrator.jobs import batch_post_close
from akq_agents.orchestrator.state_store import SchedulerStateStore


@pytest.fixture
def store(tmp_path: Path) -> SchedulerStateStore:
    return SchedulerStateStore(tmp_path / "meta.db")


@pytest.fixture
def runner(store: SchedulerStateStore) -> JobRunner:
    return JobRunner(
        store,
        is_trading_day=lambda _d: True,
        executor=ThreadPoolExecutor(max_workers=1),
    )


def test_run_once_now_writes_completed_event(
    runner: JobRunner, store: SchedulerStateStore
) -> None:
    workflow = MagicMock()
    workflow.run_once.return_value = {
        "portfolio-agent": {"portfolio_size": 50},
        "analyst-agent": {"rendered": "今日组合 50 只"},
    }
    services: dict[str, Any] = {"workflow": workflow}
    cfg = SchedulerConfig()

    batch_post_close.run_once_now(runner, services, cfg)

    # workflow.run_once 被调用一次
    workflow.run_once.assert_called_once()

    # 看 job_runs：应有一条 status='ok'
    runs = store.list_recent_runs(limit=5, job_id="batch.post_close")
    assert len(runs) == 1
    assert runs[0].status == "ok"

    # 看 events：应有 batch.post_close.completed
    events = store.list_events(limit=5)
    assert any(e.kind == "batch.post_close.completed" for e in events)

    # payload 摘要包含 portfolio_n
    payload = json.loads(runs[0].payload_json or "{}")
    assert payload["portfolio_n"] == 50


def test_run_once_now_idempotent_same_day(
    runner: JobRunner, store: SchedulerStateStore
) -> None:
    workflow = MagicMock()
    workflow.run_once.return_value = {"analyst-agent": {"rendered": "x"}}
    services: dict[str, Any] = {"workflow": workflow}
    cfg = SchedulerConfig()

    batch_post_close.run_once_now(runner, services, cfg)
    batch_post_close.run_once_now(runner, services, cfg)

    assert workflow.run_once.call_count == 1  # 第二次 noop
    runs = store.list_recent_runs(limit=5, job_id="batch.post_close")
    assert len(runs) == 1
    assert runs[0].status == "ok"


def test_run_once_now_workflow_exception_records_failed(
    runner: JobRunner, store: SchedulerStateStore
) -> None:
    workflow = MagicMock()
    workflow.run_once.side_effect = RuntimeError("workflow exploded")
    services: dict[str, Any] = {"workflow": workflow}
    cfg = SchedulerConfig()

    batch_post_close.run_once_now(runner, services, cfg)

    runs = store.list_recent_runs(limit=5, job_id="batch.post_close")
    assert len(runs) == 1
    assert runs[0].status == "failed"

    events = store.list_events(limit=5)
    fail_evt = next((e for e in events if e.kind == "batch.post_close.failed"), None)
    assert fail_evt is not None
    assert fail_evt.level == "error"


def test_run_once_now_portfolio_skipped_records_skipped(
    runner: JobRunner, store: SchedulerStateStore
) -> None:
    """当 portfolio-agent 因数据未就绪 skipped 时，整个 batch 应记为 SKIPPED 而不是 OK。

    防止之前的 silent failure：post_close 早于 data refresh 跑时，portfolio-agent
    用 fallback 数据 silently 退化但 batch 报 OK，让人误以为今天活儿干完了。
    """
    workflow = MagicMock()
    workflow.run_once.return_value = {
        "portfolio-agent": {"status": "skipped", "reason": "data_not_ready", "portfolio_size": 0},
    }
    services: dict[str, Any] = {"workflow": workflow}
    cfg = SchedulerConfig()

    batch_post_close.run_once_now(runner, services, cfg)

    runs = store.list_recent_runs(limit=5, job_id="batch.post_close")
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert runs[0].reason_code == "DATA_NOT_READY"
