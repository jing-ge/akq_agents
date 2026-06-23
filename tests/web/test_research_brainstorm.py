"""LLM brainstorm web 端点测试。复用 tests/web/conftest.py 的 client/assets fixture。"""
from unittest.mock import MagicMock

import pytest

from akq_agents.services.factors.proposal_store import (
    FactorProposal,
    FactorProposalStore,
    now_iso,
    recipe_to_json,
)


@pytest.fixture
def container_with_brainstorm(assets):
    """扩展 conftest 的 container：加 proposal_store + workflow.services + job_runner."""
    from akq_agents.web.deps import get_services

    container = assets["container"]
    db = assets["db"]
    # 真实 proposal_store（共用同一份 meta.db）
    store = FactorProposalStore(db)
    container.proposal_store = store
    # mock workflow.services（brainstormer 让 endpoint 能找到）
    fake_brainstormer = MagicMock()
    fake_brainstormer.run.return_value = {
        "requested": 5,
        "accepted_into_review": 3,
        "invalid": 1,
        "duplicate": 1,
        "errors": 0,
    }
    fake_runner = MagicMock()

    # C5: job_runner.run(job_id, partition, fn, timeout_s) → 直接执行 fn 并塞到 mock JobRunResult
    def _run(job_id, partition, fn, *, timeout_s):
        result = MagicMock()
        result.status = "ok"  # JobRunResult 用 'ok' 不是 'success'
        result.reason_code = None
        result.payload = fn()  # 调 fn() 才能让 brainstormer.run 真被调
        return result

    fake_runner.run.side_effect = _run
    # C5: job_runner 现在是 ServiceContainer 顶层字段
    container.job_runner = fake_runner
    container.workflow = MagicMock()
    container.workflow.services = {
        "llm_factor_brainstormer": fake_brainstormer,
        "factor_proposal_store": store,
    }
    return {"container": container, "store": store, "brainstormer": fake_brainstormer, "runner": fake_runner}



def test_list_llm_suggestions_empty(client, container_with_brainstorm) -> None:
    r = client.get("/api/research/factors/llm-suggestions")
    assert r.status_code == 200
    assert r.json() == {"suggestions": [], "n": 0}



def test_list_llm_suggestions_returns_recent(client, container_with_brainstorm) -> None:
    store = container_with_brainstorm["store"]
    store.upsert(
        FactorProposal(
            factor_name="llm_zscore_close_30_long_abc123",
            recipe_json=recipe_to_json({"base": "close", "op": "zscore", "window": 30, "direction": "long"}),
            direction="long",
            status="llm_suggested",
            ic_mean=None,
            ic_std=None,
            ir=None,
            t_stat=None,
            max_abs_corr=None,
            reason="LLM suggested: 中期 zscore 信号",
            created_at=now_iso(),
            evaluated_at=None,
        )
    )
    r = client.get("/api/research/factors/llm-suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 1
    assert body["suggestions"][0]["factor_name"] == "llm_zscore_close_30_long_abc123"
    assert body["suggestions"][0]["recipe"]["op"] == "zscore"



def test_brainstorm_run_invokes_brainstormer(client, container_with_brainstorm) -> None:
    r = client.post("/api/research/factors/brainstorm/run", json={"n": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    container_with_brainstorm["brainstormer"].run.assert_called_once_with(n=5)



def test_accept_llm_suggestion_changes_status_to_shadow(client, container_with_brainstorm) -> None:
    store = container_with_brainstorm["store"]
    store.upsert(
        FactorProposal(
            factor_name="llm_test_001",
            recipe_json=recipe_to_json({"base": "close", "op": "zscore", "window": 30, "direction": "long"}),
            direction="long",
            status="llm_suggested",
            ic_mean=None,
            ic_std=None,
            ir=None,
            t_stat=None,
            max_abs_corr=None,
            reason="LLM: test",
            created_at=now_iso(),
            evaluated_at=None,
        )
    )

    r = client.post("/api/research/factors/llm-suggestions/llm_test_001/accept")
    assert r.status_code == 200
    assert r.json()["status"] == "shadow"

    shadow_rows = store.list_recent(status="shadow")
    assert any(x.factor_name == "llm_test_001" for x in shadow_rows)



def test_reject_llm_suggestion_changes_status_to_rejected(client, container_with_brainstorm) -> None:
    store = container_with_brainstorm["store"]
    store.upsert(
        FactorProposal(
            factor_name="llm_test_002",
            recipe_json="{}",
            direction="long",
            status="llm_suggested",
            ic_mean=None,
            ic_std=None,
            ir=None,
            t_stat=None,
            max_abs_corr=None,
            reason="x",
            created_at=now_iso(),
            evaluated_at=None,
        )
    )
    r = client.post("/api/research/factors/llm-suggestions/llm_test_002/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"



def test_review_unknown_factor_returns_404(client, container_with_brainstorm) -> None:
    r = client.post("/api/research/factors/llm-suggestions/no_such_factor/accept")
    assert r.status_code == 404



def test_review_wrong_action_returns_400(client, container_with_brainstorm) -> None:
    r = client.post("/api/research/factors/llm-suggestions/anything/wrongaction")
    assert r.status_code == 400
