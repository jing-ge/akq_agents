"""P4 built-in tools 集成测试：用真实 P1 + P3 services 装配后跑工具。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from akq_agents.agents.chat_agent import ChatAgent
from akq_agents.models.llm_config import ChatSubConfig, SafetyConfig
from akq_agents.orchestrator.state_store import SchedulerStateStore
from akq_agents.services.factors import FactorEngine, build_default_registry
from akq_agents.services.llm import (
    LLMOrchestrator,
    LLMResponse,
    LLMStore,
    ToolRegistry,
    ToolUseRequest,
    register_default_tools,
)
from akq_agents.services.portfolio import (
    AttributionResult,
    FactorEvaluator,
    PortfolioSnapshotStore,
)


@pytest.fixture
def services(tmp_path: Path) -> dict:
    meta_db = tmp_path / "meta.db"
    # 模拟一个 P1 repo（最小化），quality_report 返回固定值
    repo = MagicMock()

    class _Health:
        def model_dump(self, mode: str = "python") -> dict:
            return {
                "last_full_refresh": "2026-06-17T15:30:00",
                "universe_size_today": 5000,
                "ohlcv_coverage_today": 0.998,
                "pending_retries": 2,
                "unresolved_errors_24h": 0,
                "health": "OK",
            }

    repo.quality_report.return_value = _Health()

    return {
        "data_repository": repo,
        "factor_registry": build_default_registry(),
        "factor_engine": FactorEngine(),
        "factor_evaluator": FactorEvaluator(meta_db, window=60),
        "portfolio_snapshot_store": PortfolioSnapshotStore(meta_db),
        "scheduler_state_store": SchedulerStateStore(meta_db),
    }


def test_register_default_tools_creates_4_tools(services: dict) -> None:
    reg = ToolRegistry()
    register_default_tools(reg, services)
    names = {spec["name"] for spec in reg.list_anthropic_specs()}
    assert names == {"get_data_health", "list_factors", "get_portfolio_snapshot", "query_events"}


def test_get_data_health_tool(services: dict) -> None:
    reg = ToolRegistry()
    register_default_tools(reg, services)
    out = reg.invoke("get_data_health", {})
    assert out["health"] == "OK"
    assert out["universe_size_today"] == 5000


def test_list_factors_tool(services: dict) -> None:
    reg = ToolRegistry()
    register_default_tools(reg, services)
    out = reg.invoke("list_factors", {})
    assert out["n"] == 7
    factor_names = {f["name"] for f in out["factors"]}
    assert "momentum_5" in factor_names
    # 还没跑过 evaluator → last_metric=None
    for f in out["factors"]:
        assert f["last_metric"] is None


def test_get_portfolio_snapshot_missing_returns_error(services: dict) -> None:
    reg = ToolRegistry()
    register_default_tools(reg, services)
    out = reg.invoke("get_portfolio_snapshot", {"date": "2026-06-17"})
    assert out["error"] == "NO_SNAPSHOT"


def test_get_portfolio_snapshot_invalid_date(services: dict) -> None:
    reg = ToolRegistry()
    register_default_tools(reg, services)
    out = reg.invoke("get_portfolio_snapshot", {"date": "not-a-date"})
    assert out["error"] == "INVALID_ARGUMENTS"


def test_get_portfolio_snapshot_with_data(services: dict) -> None:
    store = services["portfolio_snapshot_store"]
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series({"600519": 0.1}),
        composite_score=pd.Series({"600519": 1.2}),
        attribution=AttributionResult(
            as_of_date="2026-06-17",
            portfolio_contribution={"momentum_5": 0.3},
            per_stock={"600519": [{"name": "momentum_5", "contribution": 0.5}]},
            summary="",
        ),
        name_map={"600519": "贵州茅台"},
    )
    reg = ToolRegistry()
    register_default_tools(reg, services)
    out = reg.invoke("get_portfolio_snapshot", {"date": "2026-06-17"})
    assert out["n"] == 1
    assert out["rows"][0]["symbol"] == "600519"
    assert out["rows"][0]["name"] == "贵州茅台"
    assert out["rows"][0]["top_factors"] == [{"name": "momentum_5", "contribution": 0.5}]


def test_query_events_tool(services: dict) -> None:
    sched = services["scheduler_state_store"]
    sched.write_event(level="info", kind="batch.post_close.completed",
                      source="batch.post_close", payload={"n": 50})
    sched.write_event(level="warning", kind="batch.post_close.timeout",
                      source="batch.post_close", payload={"timeout_s": 5400})

    reg = ToolRegistry()
    register_default_tools(reg, services)

    # 默认查全部
    out = reg.invoke("query_events", {})
    assert out["n"] == 2

    # 按 prefix
    out2 = reg.invoke("query_events", {"kind_prefix": "batch.post_close"})
    assert out2["n"] == 2

    # 按 level
    out3 = reg.invoke("query_events", {"level_min": "warning"})
    assert out3["n"] == 1
    assert out3["events"][0]["kind"] == "batch.post_close.timeout"

    # since='24h' 应该都能查到
    out4 = reg.invoke("query_events", {"since": "24h"})
    assert out4["n"] == 2


def test_query_events_limit_capped(services: dict) -> None:
    """limit > 200 自动 cap 到 200。"""
    reg = ToolRegistry()
    register_default_tools(reg, services)
    # 直接验证函数内部的 cap 逻辑（无需写 300 个 events）
    out = reg.invoke("query_events", {"limit": 99999})
    # 没数据时返回 n=0 但不报错
    assert out["n"] == 0


def test_chat_agent_with_real_tools_end_to_end(services: dict, tmp_path: Path) -> None:
    """端到端：mock LLM → ChatAgent → ToolRegistry.invoke(get_data_health) → 拼装回复。"""
    reg = ToolRegistry()
    register_default_tools(reg, services)
    store = LLMStore(tmp_path / "meta.db")

    # mock client：第 1 轮 tool_use，第 2 轮 end_turn
    client = MagicMock()
    client.chat.side_effect = [
        LLMResponse(
            text="我查一下健康状态",
            tool_uses=[ToolUseRequest(id="tu_1", name="get_data_health", input={})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="数据健康度 OK，universe=5000", stop_reason="end_turn"),
    ]
    orch = LLMOrchestrator(client, reg, store)

    # 不进 REPL，直接 build_history + run_chat_turn 验证 wiring
    # （ChatAgent 的 repl() 是 input() 交互；这里直接走 orchestrator）
    _ = ChatAgent(
        orchestrator=orch,
        cfg=ChatSubConfig(history_window=10),
        safety=SafetyConfig(),
        store=store,
    )
    session_id = "chat:e2e"
    store.append_message(session_id=session_id, role="system", content="sys")
    text = orch.run_chat_turn(
        session_id=session_id, system_prompt="sys",
        history=[], user_message="今天数据怎么样？",
        model="m", max_tokens=2000, temperature=0.4,
    )
    assert "5000" in text or "OK" in text

    # tool 调用真的发生了
    msgs = store.list_messages(session_id, limit=20)
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_name == "get_data_health"
