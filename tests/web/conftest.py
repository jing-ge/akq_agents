"""P5 Web 控制台 conftest：注入 ServiceContainer + 提供 TestClient。"""

from __future__ import annotations

import warnings

# silence deprecation warning from starlette testclient using httpx
warnings.filterwarnings("ignore", category=DeprecationWarning, module="starlette")

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from akq_agents.models.llm_config import LLMConfig  # noqa: E402
from akq_agents.models.web_config import WebConfig  # noqa: E402
from akq_agents.orchestrator.daemon_state_file import DaemonState, DaemonStateFile  # noqa: E402
from akq_agents.orchestrator.state_store import SchedulerStateStore  # noqa: E402
from akq_agents.services.factors import build_default_registry  # noqa: E402
from akq_agents.services.llm import LLMStore  # noqa: E402
from akq_agents.services.portfolio import (  # noqa: E402
    FactorEvaluator,
    PortfolioSnapshotStore,
)
from akq_agents.web.app import create_app  # noqa: E402
from akq_agents.web.deps import ServiceContainer, set_container  # noqa: E402


@pytest.fixture
def assets(tmp_path: Path) -> Iterator[dict]:
    """构造一个完整的 ServiceContainer（mock repo / 真实 stores）。"""
    db = tmp_path / "meta.db"

    repo = MagicMock()

    class _Health:
        def model_dump(self, mode: str = "python") -> dict:
            return {
                "last_full_refresh": "2026-06-17T15:30:00",
                "universe_size_today": 5000,
                "ohlcv_coverage_today": 0.998,
                "pending_retries": 0,
                "unresolved_errors_24h": 0,
                "health": "OK",
            }

    repo.quality_report.return_value = _Health()
    repo._base_dir = tmp_path

    sched_store = SchedulerStateStore(db)
    daemon_file = DaemonStateFile(tmp_path / "daemon_state.json")
    daemon_file.write(
        DaemonState(
            status="running",
            pid=12345,
            started_at="2026-06-17T15:00:00",
            last_heartbeat="2026-06-17T15:25:00",
            version="test",
        )
    )

    container = ServiceContainer(
        repo=repo,
        sched_store=sched_store,
        daemon_state_file=daemon_file,
        factor_registry=build_default_registry(),
        factor_evaluator=FactorEvaluator(db, window=60),
        portfolio_store=PortfolioSnapshotStore(db),
        llm_orchestrator=MagicMock(),
        llm_store=LLMStore(db),
        llm_config=LLMConfig(),
        web_config=WebConfig(),
    )
    set_container(container)
    try:
        yield {"container": container, "tmp_path": tmp_path, "db": db}
    finally:
        set_container(None)


@pytest.fixture
def client(assets: dict) -> TestClient:
    app = create_app()
    return TestClient(app, client=("127.0.0.1", 12345))
