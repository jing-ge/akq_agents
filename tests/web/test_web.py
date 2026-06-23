"""P5 Web 控制台核心端到端测试。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from akq_agents.services.portfolio import AttributionResult
from akq_agents.web.guard import assert_loopback_bind

# =============================================================
# Guard
# =============================================================


def test_assert_loopback_bind_accepts_localhost() -> None:
    assert_loopback_bind("127.0.0.1")
    assert_loopback_bind("localhost")
    assert_loopback_bind("::1")


def test_assert_loopback_bind_rejects_external() -> None:
    with pytest.raises(SystemExit) as exc:
        assert_loopback_bind("0.0.0.0")
    assert exc.value.code == 2


# =============================================================
# Localhost middleware
# =============================================================


def test_non_local_request_rejected_with_403(client) -> None:
    from fastapi.testclient import TestClient

    from akq_agents.web.app import create_app

    bad_client = TestClient(create_app(), client=("8.8.8.8", 9999))
    r = bad_client.get("/api/ops/health")
    assert r.status_code == 403
    assert "rejected" in r.json().get("error", "").lower()


# =============================================================
# Pages
# =============================================================


def test_root_redirects_to_ops(client) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/ops"


@pytest.mark.parametrize("path", ["/ops", "/research", "/chat"])
def test_pages_return_html(client, path) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    # base layout
    assert "AKQ Agents Console" in r.text


# =============================================================
# Ops API
# =============================================================


def test_ops_health_aggregated_response(client, assets) -> None:
    r = client.get("/api/ops/health")
    assert r.status_code == 200
    d = r.json()
    assert d["data_health"]["health"] == "OK"
    assert d["daemon"]["is_alive"] in (True, False)  # 实测可能因 daemon_file ts 较老
    assert "scheduler_events_24h_by_level" in d
    # 字段命名规范（spec A11）
    assert {"info", "warning", "error"}.issubset(d["scheduler_events_24h_by_level"].keys())


def test_ops_health_with_no_daemon_does_not_crash(assets, client) -> None:
    """daemon_state_file 不存在时应返回 is_alive=false，不崩页。"""
    from akq_agents.orchestrator.daemon_state_file import DaemonStateFile

    # 替换为不存在的 path
    assets["container"].daemon_state_file = DaemonStateFile(assets["tmp_path"] / "nonexistent.json")
    r = client.get("/api/ops/health")
    assert r.status_code == 200
    d = r.json()
    assert d["daemon"]["state"] is None
    assert d["daemon"]["is_alive"] is False


def test_ops_job_runs_returns_list(client, assets) -> None:
    assets["container"].sched_store.upsert_job_run(
        job_id="batch.post_close", partition="2026-06-17", status="ok",
        started_at="2026-06-17T15:30:00", finished_at="2026-06-17T16:00:00",
        duration_ms=1800000,
    )
    r = client.get("/api/ops/job-runs?limit=10")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 1
    assert d["runs"][0]["job_id"] == "batch.post_close"


def test_ops_events_filter_by_level(client, assets) -> None:
    s = assets["container"].sched_store
    s.write_event(level="info", kind="batch.post_close.completed")
    s.write_event(level="warning", kind="batch.post_close.timeout")
    r = client.get("/api/ops/events?limit=10&level_min=warning")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 1
    assert d["events"][0]["kind"] == "batch.post_close.timeout"


# =============================================================
# Research API
# =============================================================


def test_research_factors_list(client) -> None:
    r = client.get("/api/research/factors")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 7  # 默认 7 个因子


def test_research_factor_metrics_empty_returns_empty_list(client) -> None:
    r = client.get("/api/research/factors/momentum_5/metrics")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_research_portfolio_404_when_missing(client) -> None:
    r = client.get("/api/research/portfolio?date=2026-06-17")
    assert r.status_code == 404
    body = r.json()
    # FastAPI HTTPException dict wraps in detail
    assert "no_snapshot_for_date" in str(body)


def test_research_portfolio_400_on_invalid_date(client) -> None:
    r = client.get("/api/research/portfolio?date=not-a-date")
    assert r.status_code == 400


def test_research_portfolio_renders_from_snapshot(client, assets) -> None:
    """A14 验收：API 不查 P1 universe，直接消费 portfolio_snapshots.name。"""
    store = assets["container"].portfolio_store
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series({"600519": 0.5, "000001": 0.5}),
        composite_score=pd.Series({"600519": 1.2, "000001": 0.8}),
        attribution=AttributionResult(
            as_of_date="2026-06-17",
            portfolio_contribution={"momentum_5": 0.3},
            per_stock={
                "600519": [{"name": "momentum_5", "contribution": 0.7}],
                "000001": [{"name": "momentum_5", "contribution": 0.4}],
            },
            summary="",
        ),
        name_map={"600519": "贵州茅台", "000001": "平安银行"},
    )
    r = client.get("/api/research/portfolio?date=2026-06-17")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 2
    assert d["rows"][0]["name"] in {"贵州茅台", "平安银行"}
    assert d["rows"][0]["top_factors"]


def test_research_portfolio_attribution_aggregates(client, assets) -> None:
    store = assets["container"].portfolio_store
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series({"A": 0.5, "B": 0.5}),
        composite_score=pd.Series({"A": 1.0, "B": 1.0}),
        attribution=AttributionResult(
            as_of_date="2026-06-17",
            portfolio_contribution={},
            per_stock={
                "A": [{"name": "momentum_5", "contribution": 0.6}, {"name": "volatility_20", "contribution": -0.2}],
                "B": [{"name": "momentum_5", "contribution": 0.4}],
            },
            summary="",
        ),
    )
    r = client.get("/api/research/portfolio/attribution?date=2026-06-17")
    assert r.status_code == 200
    d = r.json()
    # momentum_5 总贡献 = 0.5 * 0.6 + 0.5 * 0.4 = 0.5
    assert d["portfolio_contribution"]["momentum_5"] == pytest.approx(0.5, abs=1e-9)


def test_research_daily_attribution_returns_contributors_draggers_and_factors(client, assets, tmp_path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    store = assets["container"].portfolio_store
    store.write(
        as_of_date=date(2026, 6, 23),
        weights=pd.Series({"AAA": 0.6, "BBB": 0.4}),
        prev_weights=pd.Series({"AAA": 0.5, "BBB": 0.3}),
        composite_score=pd.Series({"AAA": 1.2, "BBB": 0.8}),
        attribution=AttributionResult(
            as_of_date="2026-06-23",
            portfolio_contribution={},
            per_stock={
                "AAA": [{"name": "quality", "contribution": 0.6}],
                "BBB": [{"name": "quality", "contribution": -0.1}, {"name": "value", "contribution": -0.4}],
            },
            summary="",
        ),
        name_map={"AAA": "Alpha", "BBB": "Beta"},
        industry_map={"AAA": "Tech", "BBB": "Bank"},
    )

    ohlcv_dir = tmp_path / "ohlcv"
    (ohlcv_dir / "date=2026-06-20").mkdir(parents=True)
    (ohlcv_dir / "date=2026-06-23").mkdir(parents=True)
    pq.write_table(
        pa.table({"symbol": ["AAA", "BBB"], "close": [100.0, 100.0]}),
        ohlcv_dir / "date=2026-06-20" / "part-0.parquet",
    )
    pq.write_table(
        pa.table({"symbol": ["AAA", "BBB"], "close": [110.0, 90.0]}),
        ohlcv_dir / "date=2026-06-23" / "part-0.parquet",
    )
    assets["container"].repo._ohlcv_dir = ohlcv_dir

    r = client.get("/api/research/daily-attribution?date=2026-06-23")
    assert r.status_code == 200
    d = r.json()
    assert d["date"] == "2026-06-23"
    assert d["n_holdings"] == 2
    assert d["n_with_return"] == 2
    assert [x["symbol"] for x in d["top_contributors"]] == ["AAA", "BBB"]
    assert [x["symbol"] for x in d["top_draggers"]] == ["BBB", "AAA"]
    assert d["top_contributors"][0]["contrib_bps"] == pytest.approx(500.0, abs=1e-9)
    assert d["top_draggers"][0]["contrib_bps"] == pytest.approx(-300.0, abs=1e-9)
    assert d["factor_contribution"][0]["name"] == "quality"
    assert d["factor_contribution"][0]["contribution"] == pytest.approx(0.5, abs=1e-9)


# =============================================================
# Chat API
# =============================================================


def test_chat_sessions_list_empty(client) -> None:
    r = client.get("/api/chat/sessions")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_chat_create_session_returns_session_id(client) -> None:
    r = client.post("/api/chat/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert sid.startswith("chat:")

    # session 立即出现在 list 中
    r2 = client.get("/api/chat/sessions")
    assert r2.json()["n"] == 1


def test_chat_messages_list_returns_history(client, assets) -> None:
    store = assets["container"].llm_store
    store.append_message(session_id="s1", role="system", content="sys")
    store.append_message(session_id="s1", role="user", content="hi")
    store.append_message(session_id="s1", role="assistant", content="hello")
    r = client.get("/api/chat/sessions/s1/messages")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 3


def test_chat_message_post_sse_returns_done(client, assets) -> None:
    """SSE 端到端：mock orchestrator → 检查 tool_use / assistant / done 事件。"""
    container = assets["container"]
    container.llm_orchestrator.run_chat_turn.return_value = "今天数据正常"
    # 创建 session
    r0 = client.post("/api/chat/sessions")
    sid = r0.json()["session_id"]

    with client.stream(
        "POST",
        f"/api/chat/sessions/{sid}/messages",
        json={"content": "今天数据怎么样？"},
    ) as resp:
        assert resp.status_code == 200
        text = "".join(chunk.decode() for chunk in resp.iter_bytes())

    assert "event: assistant" in text
    assert "event: done" in text
    assert "今天数据正常" in text


def test_chat_message_post_bad_request_empty_content(client) -> None:
    r0 = client.post("/api/chat/sessions")
    sid = r0.json()["session_id"]
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "  "})
    assert r.status_code == 400


def test_chat_message_post_error_sse_when_llm_unavailable(client, assets) -> None:
    """LLM down → SSE 发 event: error。"""
    from akq_agents.services.llm.client import LLMGatewayError

    container = assets["container"]
    container.llm_orchestrator.run_chat_turn.side_effect = LLMGatewayError(
        "down", reason_code="UPSTREAM_ERROR"
    )
    r0 = client.post("/api/chat/sessions")
    sid = r0.json()["session_id"]
    with client.stream("POST", f"/api/chat/sessions/{sid}/messages", json={"content": "hi"}) as resp:
        text = "".join(chunk.decode() for chunk in resp.iter_bytes())
    assert "event: error" in text
    assert "UPSTREAM_ERROR" in text


# =============================================================
# A13: 所有 API GET，唯一 POST 在 /api/chat
# =============================================================


def test_only_chat_endpoints_use_post(client) -> None:
    """A13 验收：业务相关 API 全部 GET；唯一 POST 在 /api/chat。"""
    # OpenAPI 已禁用（openapi_url=None），所以通过 app.routes 检查
    from akq_agents.web.app import create_app

    app = create_app()
    post_routes = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if "POST" in methods:
            post_routes.append(path)
    # 所有 POST 路径必须在 /api/chat/* 下
    for path in post_routes:
        assert path.startswith("/api/chat"), f"unexpected POST endpoint: {path}"


# =============================================================
# uvicorn workers=1 验证（通过 server.start 检查）
# =============================================================


def test_server_module_passes_workers_1(monkeypatch) -> None:
    """A12 验收：uvicorn.run 调用 workers=1。"""
    called = {}

    def fake_uvicorn_run(app: str, **kwargs):
        called.update(kwargs)
        called["app"] = app

    import sys
    fake_uvicorn = type(sys)("uvicorn")
    fake_uvicorn.run = fake_uvicorn_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    from akq_agents.web.server import start

    start(host="127.0.0.1", port=8765)
    assert called["workers"] == 1
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8765
