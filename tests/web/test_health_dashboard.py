"""测试 /api/research/factors/health-dashboard 端点。

补 research.py 覆盖洞: 该端点原为裸 sqlite3.connect + 内联 SQL, 收口重构为
走 FactorProposalStore / SchedulerStateStore 后, 需回归验证输出结构与语义:
- status_counts 走 store.counts() (排除 evicted)
- shadow OOS 分桶 / top-near-ready 走 store.list_shadow()
- freshness 走 sched_store.list_recent_runs()
- reject_reasons 走 open_meta_db(repo.meta_db_path) (非裸连接)
"""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="starlette")

from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from akq_agents.orchestrator.state_store import SchedulerStateStore  # noqa: E402
from akq_agents.services.factors import build_default_registry  # noqa: E402
from akq_agents.services.factors.proposal_store import (  # noqa: E402
    FactorProposal,
    FactorProposalStore,
    now_iso,
)
from akq_agents.web.app import create_app  # noqa: E402
from akq_agents.web.deps import ServiceContainer, set_container  # noqa: E402


@pytest.fixture
def dashboard_client(tmp_path: Path):
    db = tmp_path / "meta.db"
    repo = MagicMock()
    repo._base_dir = tmp_path
    repo.meta_db_path = db  # public property: 收口后端点走它, 必须是真实路径

    sched = SchedulerStateStore(db)
    sched.upsert_job_run(
        job_id="factor.discovery", partition="2026-07-07", status="success",
        reason_code=None, started_at="2026-07-07T20:00:00",
        finished_at="2026-07-07T20:01:00", duration_ms=60000,
    )

    pstore = FactorProposalStore(db)
    pstore.upsert(FactorProposal(
        factor_name="shadow1", status="shadow", oos_observations=8,
        shadow_started_at=now_iso(), created_at=now_iso(),
    ))
    pstore.upsert(FactorProposal(
        factor_name="rej1", status="rejected", reason="low_ic: 0.01", created_at=now_iso(),
    ))

    container = ServiceContainer(
        repo=repo, sched_store=sched, proposal_store=pstore,
        factor_registry=build_default_registry(),
    )
    set_container(container)
    try:
        app = create_app()
        yield TestClient(app, client=("127.0.0.1", 12345))
    finally:
        set_container(None)


def test_health_dashboard_returns_expected_structure(dashboard_client):
    r = dashboard_client.get("/api/research/factors/health-dashboard")
    assert r.status_code == 200
    d = r.json()
    # 所有 7 个字段都在
    for key in ["status_counts", "shadow_oos_buckets", "shadow_top_near_ready",
                "selected_metrics", "decay_global", "freshness", "reject_reasons"]:
        assert key in d

    # status_counts: shadow=1, rejected=1
    assert d["status_counts"].get("shadow") == 1
    assert d["status_counts"].get("rejected") == 1

    # shadow OOS: oos=8 落在 6-10 桶
    assert d["shadow_oos_buckets"]["6-10"] == 1
    assert d["shadow_oos_buckets"]["0-5"] == 0

    # top-near-ready: 1 个 shadow, days_to_ready = 20-8 = 12
    assert len(d["shadow_top_near_ready"]) == 1
    assert d["shadow_top_near_ready"][0]["name"] == "shadow1"
    assert d["shadow_top_near_ready"][0]["days_to_ready"] == 12

    # freshness: factor.discovery 有记录, 其余 never_run
    disc = next(f for f in d["freshness"] if f["job_id"] == "factor.discovery")
    assert disc["status"] == "success"

    # reject_reasons: low_ic 归类
    assert d["reject_reasons"].get("low_ic") == 1


def test_health_dashboard_excludes_evicted(dashboard_client, tmp_path):
    """收口后 status_counts 走 store.counts() — 被 evict 的因子不计入 (语义修正)。"""
    # 直接标记 shadow1 为 evicted
    from akq_agents.services.data.repository import open_meta_db
    with open_meta_db(tmp_path / "meta.db") as conn:
        conn.execute("UPDATE factor_proposals SET evicted_at=? WHERE factor_name='shadow1'", (now_iso(),))
        conn.commit()

    r = dashboard_client.get("/api/research/factors/health-dashboard")
    assert r.status_code == 200
    d = r.json()
    # evicted 的 shadow1 不再计入 shadow 计数 / 分桶
    assert d["status_counts"].get("shadow", 0) == 0
    assert d["shadow_oos_buckets"]["6-10"] == 0
    assert len(d["shadow_top_near_ready"]) == 0
