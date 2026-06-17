"""P4 LLM 层单元测试：Client / Registry / Store / Orchestrator / AnalystAgent / ChatAgent。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from akq_agents.services.llm import (
    GatewayLLMClient,
    LLMGatewayConfig,
    LLMGatewayError,
    LLMOrchestrator,
    LLMResponse,
    LLMStore,
    ToolRegistry,
    ToolSpec,
    ToolUseRequest,
)

# =============================================================
# GatewayLLMClient
# =============================================================


def _make_response(content_blocks: list[dict], stop_reason: str = "end_turn") -> bytes:
    return json.dumps(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ).encode()


class _FakeUrlopenResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self._status

    def read(self):
        return self._body


def test_gateway_parses_text_response() -> None:
    body = _make_response([{"type": "text", "text": "你好"}])
    with patch("urllib.request.urlopen", return_value=_FakeUrlopenResp(body)):
        resp = GatewayLLMClient(LLMGatewayConfig(max_retries=0)).chat(
            model="Claude-Opus-4.7", system="s", messages=[{"role": "user", "content": "x"}],
            max_tokens=100, temperature=0.1,
        )
    assert resp.text == "你好"
    assert resp.stop_reason == "end_turn"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5


def test_gateway_parses_tool_use_response() -> None:
    body = _make_response(
        [
            {"type": "text", "text": "我来查一下"},
            {"type": "tool_use", "id": "tu_1", "name": "get_data_health", "input": {}},
        ],
        stop_reason="tool_use",
    )
    with patch("urllib.request.urlopen", return_value=_FakeUrlopenResp(body)):
        resp = GatewayLLMClient(LLMGatewayConfig(max_retries=0)).chat(
            model="m", system="s", messages=[{"role": "user", "content": "x"}],
            max_tokens=100, temperature=0.1,
        )
    assert len(resp.tool_uses) == 1
    assert resp.tool_uses[0].name == "get_data_health"
    assert resp.stop_reason == "tool_use"


def test_gateway_429_raises_rate_limited() -> None:
    import urllib.error

    err = urllib.error.HTTPError("http://x", 429, "rate", {}, None)  # type: ignore[arg-type]
    err.read = lambda: b"slow down"  # type: ignore[method-assign]
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(LLMGatewayError) as exc_info:
            GatewayLLMClient(LLMGatewayConfig(max_retries=0)).chat(
                model="m", system="s", messages=[{"role": "user", "content": "x"}],
                max_tokens=100, temperature=0.1,
            )
    assert exc_info.value.reason_code == "RATE_LIMITED"


def test_gateway_5xx_raises_upstream() -> None:
    import urllib.error

    err = urllib.error.HTTPError("http://x", 503, "down", {}, None)  # type: ignore[arg-type]
    err.read = lambda: b"upstream down"  # type: ignore[method-assign]
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(LLMGatewayError) as exc_info:
            GatewayLLMClient(LLMGatewayConfig(max_retries=0)).chat(
                model="m", system="s", messages=[{"role": "user", "content": "x"}],
                max_tokens=100, temperature=0.1,
            )
    assert exc_info.value.reason_code == "UPSTREAM_ERROR"


# =============================================================
# ToolRegistry
# =============================================================


def test_registry_rejects_non_read_only() -> None:
    reg = ToolRegistry()
    spec = ToolSpec(
        name="bad",
        description="d",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda _: {},
        read_only=False,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="read_only"):
        reg.register(spec)


def test_registry_invoke_validates_args() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="d",
            json_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            handler=lambda args: {"echo": args["x"]},
        )
    )
    # missing required → INVALID_ARGUMENTS
    assert reg.invoke("echo", {})["error"] == "INVALID_ARGUMENTS"
    # wrong type → INVALID_ARGUMENTS
    assert reg.invoke("echo", {"x": 123})["error"] == "INVALID_ARGUMENTS"
    # ok
    assert reg.invoke("echo", {"x": "hi"}) == {"echo": "hi"}


def test_registry_invoke_unknown_returns_error() -> None:
    reg = ToolRegistry()
    out = reg.invoke("nonexistent", {})
    assert out["error"] == "TOOL_NOT_FOUND"


def test_registry_invoke_handler_exception_returns_internal() -> None:
    reg = ToolRegistry()

    def boom(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    reg.register(
        ToolSpec(
            name="boom",
            description="d",
            json_schema={"type": "object", "properties": {}, "required": []},
            handler=boom,
        )
    )
    out = reg.invoke("boom", {})
    assert out["error"] == "INTERNAL"
    assert "kaboom" in out["message"]


def test_registry_truncates_large_results() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="big",
            description="d",
            json_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: {"data": "x" * 20000},
            truncate_chars=1000,
        )
    )
    out = reg.invoke("big", {})
    assert out.get("_truncated") is True
    assert "truncated" in out.get("summary", "").lower()


def test_registry_anthropic_specs_shape() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="t1",
            description="desc",
            json_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: {},
        )
    )
    specs = reg.list_anthropic_specs()
    assert specs == [
        {"name": "t1", "description": "desc", "input_schema": {"type": "object", "properties": {}, "required": []}}
    ]


def test_registry_duplicate_raises() -> None:
    reg = ToolRegistry()
    spec = ToolSpec(
        name="t",
        description="d",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda _: {},
    )
    reg.register(spec)
    with pytest.raises(ValueError, match="already"):
        reg.register(spec)


# =============================================================
# LLMStore
# =============================================================


def test_store_insert_and_list_calls(tmp_path: Path) -> None:
    store = LLMStore(tmp_path / "meta.db")
    store.insert_call(agent="analyst", session_id="s1", model="Claude-Opus-4.7",
                     prompt_tokens=100, completion_tokens=50, latency_ms=2000,
                     status="ok")
    calls = store.list_calls(limit=10)
    assert len(calls) == 1
    assert calls[0].agent == "analyst"
    assert calls[0].status == "ok"


def test_store_messages_roundtrip(tmp_path: Path) -> None:
    store = LLMStore(tmp_path / "meta.db")
    store.append_message(session_id="s1", role="user", content="问题 1")
    store.append_message(session_id="s1", role="assistant", content="回答 1")
    store.append_message(
        session_id="s1",
        role="tool",
        content="get_data_health",
        tool_name="get_data_health",
        tool_args={},
        tool_result={"health": "OK"},
    )
    msgs = store.list_messages("s1", limit=10)
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[-1].tool_name == "get_data_health"
    assert json.loads(msgs[-1].tool_result or "{}")["health"] == "OK"


def test_store_recent_messages_returns_ascending_order(tmp_path: Path) -> None:
    store = LLMStore(tmp_path / "meta.db")
    for i in range(5):
        store.append_message(session_id="s1", role="user", content=f"msg {i}")
    recent = store.recent_messages("s1", limit=3)
    assert [m.content for m in recent] == ["msg 2", "msg 3", "msg 4"]


def test_store_list_sessions_grouped(tmp_path: Path) -> None:
    store = LLMStore(tmp_path / "meta.db")
    store.append_message(session_id="A", role="user", content="x")
    store.append_message(session_id="B", role="user", content="y")
    store.append_message(session_id="B", role="user", content="z")
    sessions = store.list_sessions(limit=10)
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["A"]["message_count"] == 1
    assert by_id["B"]["message_count"] == 2


# =============================================================
# LLMOrchestrator
# =============================================================


def _mock_client(responses: list[LLMResponse]) -> MagicMock:
    """让 mock client.chat 依次返回 responses 列表中的元素。"""
    client = MagicMock()
    client.chat.side_effect = responses
    return client


def test_orchestrator_analyst_no_tools_passed(tmp_path: Path) -> None:
    """A6 验收：AnalystAgent 不传 tools 字段给 LLM。"""
    store = LLMStore(tmp_path / "meta.db")
    reg = ToolRegistry()
    client = _mock_client([LLMResponse(text="盘后简评内容", stop_reason="end_turn")])
    orch = LLMOrchestrator(client, reg, store)

    text = orch.run_analyst(
        session_id="analyst:2026-06-17", system_prompt="s",
        user_message="payload", model="Claude-Opus-4.7", max_tokens=4000, temperature=0.2,
    )
    assert text == "盘后简评内容"

    # 验证 tools=None
    call_kwargs = client.chat.call_args.kwargs
    assert call_kwargs["tools"] is None

    # llm_calls 表中有一条 analyst record
    calls = store.list_calls(agent="analyst")
    assert len(calls) == 1
    assert calls[0].status == "ok"


def test_orchestrator_analyst_propagates_gateway_error(tmp_path: Path) -> None:
    store = LLMStore(tmp_path / "meta.db")
    client = MagicMock()
    client.chat.side_effect = LLMGatewayError("down", reason_code="UPSTREAM_ERROR")
    orch = LLMOrchestrator(client, ToolRegistry(), store)
    with pytest.raises(LLMGatewayError):
        orch.run_analyst(
            session_id="x", system_prompt="s", user_message="u",
            model="m", max_tokens=10, temperature=0.1,
        )
    calls = store.list_calls()
    assert calls[0].status == "failed"
    assert calls[0].reason_code == "UPSTREAM_ERROR"


def test_orchestrator_chat_tool_loop_completes(tmp_path: Path) -> None:
    """LLM 第一轮 tool_use → 第二轮 end_turn 输出最终回答。"""
    store = LLMStore(tmp_path / "meta.db")
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="get_data_health",
            description="d",
            json_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: {"health": "OK", "universe_size_today": 5000},
        )
    )
    client = _mock_client(
        [
            LLMResponse(
                text="我先查一下健康状态",
                tool_uses=[ToolUseRequest(id="tu_1", name="get_data_health", input={})],
                stop_reason="tool_use",
            ),
            LLMResponse(text="当前数据状态健康，universe 5000 只", stop_reason="end_turn"),
        ]
    )
    orch = LLMOrchestrator(client, reg, store, max_iterations=6)

    text = orch.run_chat_turn(
        session_id="chat:abc", system_prompt="s", history=[],
        user_message="今天数据怎么样？",
        model="m", max_tokens=2000, temperature=0.4,
    )
    assert "5000" in text or "健康" in text
    # 应该发起 2 次 LLM 调用
    assert client.chat.call_count == 2

    # chat_messages 表：user / assistant(text+tool_use) / tool / assistant(final)
    msgs = store.list_messages("chat:abc", limit=20)
    roles = [m.role for m in msgs]
    assert roles.count("user") == 1
    assert roles.count("tool") == 1
    # 2 个 assistant 调用 → store 写 2 个 assistant 消息 + 最后一个 final assistant
    assert "assistant" in roles


def test_orchestrator_chat_max_iterations_truncates(tmp_path: Path) -> None:
    """LLM 一直 tool_use → 达到 max_iterations 自动截断。"""
    store = LLMStore(tmp_path / "meta.db")
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="infinite",
            description="d",
            json_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda _: {"x": 1},
        )
    )
    # 总是 tool_use
    responses = [
        LLMResponse(
            text=f"iter {i}",
            tool_uses=[ToolUseRequest(id=f"tu_{i}", name="infinite", input={})],
            stop_reason="tool_use",
        )
        for i in range(10)
    ]
    client = _mock_client(responses)
    orch = LLMOrchestrator(client, reg, store, max_iterations=3)

    text = orch.run_chat_turn(
        session_id="x", system_prompt="s", history=[], user_message="u",
        model="m", max_tokens=100, temperature=0.4,
    )
    assert "上限" in text
    assert client.chat.call_count == 3

    calls = store.list_calls(agent="chat")
    # 最后一条状态 truncated
    assert calls[0].status == "truncated"
    assert calls[0].reason_code == "TOOL_LOOP_EXCEEDED"


# =============================================================
# AnalystAgent
# =============================================================


def test_analyst_agent_writes_report(tmp_path: Path) -> None:
    from akq_agents.agents.analyst_agent import AnalystAgent
    from akq_agents.agents.base import AgentContext
    from akq_agents.models.llm_config import AnalystSubConfig, SafetyConfig

    store = LLMStore(tmp_path / "meta.db")
    client = _mock_client([LLMResponse(text="## 数据状态\n5000 只\n\n## 组合概览\n50 只", stop_reason="end_turn")])
    orch = LLMOrchestrator(client, ToolRegistry(), store)

    agent = AnalystAgent(
        orchestrator=orch,
        cfg=AnalystSubConfig(),
        reports_dir=tmp_path / "reports",
        safety=SafetyConfig(),
    )
    ctx = AgentContext(state={
        "today": "2026-06-17",
        "portfolio": [{"symbol": "600519", "weight": 0.1, "score": 1.5}],
        "attribution": {"portfolio_contribution": {"momentum_5": 0.3}},
        "data_health": {"health": "OK", "universe_size_today": 5000},
        "portfolio_turnover": 0.18,
    })

    result = agent.run(ctx)
    assert result["status"] == "ok"
    path = Path(result["path"])
    assert path.exists()
    content = path.read_text()
    # disclaimer 首行
    assert content.startswith(">")
    assert "5000" in content


def test_analyst_agent_fallback_on_llm_failure(tmp_path: Path) -> None:
    from akq_agents.agents.analyst_agent import AnalystAgent
    from akq_agents.agents.base import AgentContext
    from akq_agents.models.llm_config import AnalystSubConfig, SafetyConfig

    store = LLMStore(tmp_path / "meta.db")
    client = MagicMock()
    client.chat.side_effect = LLMGatewayError("network down", reason_code="UPSTREAM_ERROR")
    orch = LLMOrchestrator(client, ToolRegistry(), store)

    agent = AnalystAgent(
        orchestrator=orch,
        cfg=AnalystSubConfig(),
        reports_dir=tmp_path / "reports",
        safety=SafetyConfig(),
    )
    ctx = AgentContext(state={"today": "2026-06-17", "portfolio": [], "daily_advice": {"rendered": "ok"}})
    result = agent.run(ctx)
    assert result["status"] == "degraded"
    path = Path(result["path"])
    assert path.exists()
    assert "退化" in path.read_text() or "LLM" in path.read_text()


def test_analyst_agent_skipped_when_disabled(tmp_path: Path) -> None:
    from akq_agents.agents.analyst_agent import AnalystAgent
    from akq_agents.agents.base import AgentContext
    from akq_agents.models.llm_config import AnalystSubConfig, SafetyConfig

    store = LLMStore(tmp_path / "meta.db")
    orch = LLMOrchestrator(MagicMock(), ToolRegistry(), store)
    agent = AnalystAgent(
        orchestrator=orch,
        cfg=AnalystSubConfig(enabled=False),
        reports_dir=tmp_path / "reports",
        safety=SafetyConfig(),
    )
    result = agent.run(AgentContext(state={}))
    assert result["status"] == "skipped"


def test_analyst_agent_truncates_top_holdings(tmp_path: Path) -> None:
    """spec A10：top 20 持仓限制。"""
    from akq_agents.agents.analyst_agent import AnalystAgent
    from akq_agents.agents.base import AgentContext
    from akq_agents.models.llm_config import AnalystSubConfig, SafetyConfig

    store = LLMStore(tmp_path / "meta.db")
    client = _mock_client([LLMResponse(text="ok", stop_reason="end_turn")])
    orch = LLMOrchestrator(client, ToolRegistry(), store)
    agent = AnalystAgent(
        orchestrator=orch,
        cfg=AnalystSubConfig(context_top_holdings=20),
        reports_dir=tmp_path / "reports",
        safety=SafetyConfig(),
    )
    ctx = AgentContext(state={
        "today": "2026-06-17",
        "portfolio": [{"symbol": f"S{i:03d}", "weight": (100 - i) * 0.01, "score": 1.0} for i in range(100)],
        "attribution": {},
        "data_health": {},
    })
    agent.run(ctx)
    # 抓 prompt 看 portfolio_top 长度
    user_msg = client.chat.call_args.kwargs["messages"][0]["content"]
    payload = json.loads(user_msg.split("```json", 1)[1].split("```", 1)[0])
    assert len(payload["portfolio_top"]) == 20
    assert payload["portfolio_n"] == 100
