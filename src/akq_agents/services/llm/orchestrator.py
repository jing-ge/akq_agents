"""LLMOrchestrator：单次 analyst 调用 + chat tool loop。

边界（spec v2 关键边界）：
- 工具调用本身 read-only（由 ToolRegistry 启动期强校验）
- Orchestrator 写 ``llm_calls`` / ``chat_messages`` 是 **基础设施层**，不受 read-only 约束
"""

from __future__ import annotations

import logging
import time
from typing import Any

from akq_agents.services.llm.client import LLMClient, LLMGatewayError, LLMResponse
from akq_agents.services.llm.store import LLMStore
from akq_agents.services.llm.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class LLMOrchestrator:
    """单次 analyst 调用（无 tools） + chat tool loop。"""

    def __init__(
        self,
        client: LLMClient,
        tools: ToolRegistry,
        store: LLMStore,
        *,
        max_iterations: int = 6,
    ) -> None:
        self._client = client
        self._tools = tools
        self._store = store
        self._max_iters = max_iterations

    # ---------------- analyst path（无 ToolUse） ----------------

    def run_analyst(
        self,
        *,
        session_id: str,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout_s: int = 60,
    ) -> str:
        """单次调用，**不传 tools**；返回 LLM 文本。

        失败抛 :class:`LLMGatewayError`（不 swallow，由调用方 fallback）。
        """
        t0 = time.monotonic()
        try:
            resp = self._client.chat(
                model=model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=None,  # 关键：analyst 不使用 ToolUse
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=timeout_s,
            )
        except LLMGatewayError as exc:
            self._store.insert_call(
                agent="analyst", session_id=session_id, model=model,
                status="failed", reason_code=exc.reason_code, error_msg=str(exc)[:300],
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        self._store.insert_call(
            agent="analyst", session_id=session_id, model=model,
            prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
            latency_ms=latency_ms, tool_calls=0, status="ok",
        )
        return resp.text

    # ---------------- chat path（带 ToolUse loop） ----------------

    def run_chat_turn(
        self,
        *,
        session_id: str,
        system_prompt: str,
        history: list[dict[str, Any]],
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout_s: int = 60,
    ) -> str:
        """对话一轮 + tool loop ≤ max_iterations；返回最终 assistant text。

        history: 列表形式的 Anthropic messages（仅 role/content；system 单独传）。
        本轮的 user_message 会自动 append。本轮每条 assistant / tool 也会自动写
        ``chat_messages`` 表与 ``llm_calls``。
        """
        # 先持久化 user 消息
        self._store.append_message(session_id=session_id, role="user", content=user_message)

        messages: list[dict[str, Any]] = list(history)
        messages.append({"role": "user", "content": user_message})

        tools_spec = self._tools.list_anthropic_specs()
        if not tools_spec:
            tools_spec = None  # type: ignore[assignment]

        last_text = ""
        iters = 0
        for iter_idx in range(self._max_iters):
            iters = iter_idx + 1
            t0 = time.monotonic()
            try:
                resp = self._client.chat(
                    model=model,
                    system=system_prompt,
                    messages=messages,
                    tools=tools_spec,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_s=timeout_s,
                )
            except LLMGatewayError as exc:
                self._store.insert_call(
                    agent="chat", session_id=session_id, model=model,
                    status="failed", reason_code=exc.reason_code, error_msg=str(exc)[:300],
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
                raise

            latency_ms = int((time.monotonic() - t0) * 1000)
            n_tools = len(resp.tool_uses)
            self._store.insert_call(
                agent="chat", session_id=session_id, model=model,
                prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                latency_ms=latency_ms, tool_calls=n_tools, status="ok",
            )

            # 持久化本轮 assistant 消息（即便 tool_use_only 也写：内容含 text + 工具请求）
            self._persist_assistant(session_id, resp)

            # 把 assistant 完整 content（含 tool_use blocks）加入 messages，保留协议状态
            assistant_blocks = self._assistant_content_blocks(resp)
            messages.append({"role": "assistant", "content": assistant_blocks})

            if not resp.tool_uses:
                last_text = resp.text
                break

            # 执行工具，结果以 tool_result blocks 形式追加为 user 消息（Anthropic 协议要求）
            tool_results_blocks: list[dict[str, Any]] = []
            for tu in resp.tool_uses:
                result = self._tools.invoke(tu.name, tu.input, session_id=session_id)
                self._store.append_message(
                    session_id=session_id,
                    role="tool",
                    content=tu.name,
                    tool_name=tu.name,
                    tool_args=tu.input,
                    tool_result=result,
                )
                tool_results_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": _serialize_tool_result(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results_blocks})
            last_text = resp.text  # 暂存，下一轮会被覆盖
        else:
            # 跑完了 max_iterations 还在 tool_use → 截断
            self._store.insert_call(
                agent="chat", session_id=session_id, model=model,
                status="truncated", reason_code="TOOL_LOOP_EXCEEDED",
                error_msg=f"exceeded {self._max_iters} iterations",
            )
            last_text = (last_text or "") + "\n\n[已达 ToolUse 循环上限，输出可能不完整]"

        # 持久化最终 assistant
        if last_text:
            self._store.append_message(session_id=session_id, role="assistant", content=last_text)
        _ = iters
        return last_text

    # ---------------- internal helpers ----------------

    @staticmethod
    def _assistant_content_blocks(resp: LLMResponse) -> list[dict[str, Any]]:
        """重构 assistant message 的 content blocks，保持 Anthropic 协议状态。"""
        blocks: list[dict[str, Any]] = []
        if resp.text:
            blocks.append({"type": "text", "text": resp.text})
        for tu in resp.tool_uses:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tu.id,
                    "name": tu.name,
                    "input": tu.input,
                }
            )
        return blocks

    def _persist_assistant(self, session_id: str, resp: LLMResponse) -> None:
        """持久化 assistant 消息（含 tool_use 摘要）。"""
        content_text = resp.text
        if resp.tool_uses:
            tool_names = ", ".join(t.name for t in resp.tool_uses)
            content_text = (content_text + f"\n\n[tool_use: {tool_names}]").strip()
        self._store.append_message(session_id=session_id, role="assistant", content=content_text)


def _serialize_tool_result(result: dict[str, Any]) -> str:
    """Anthropic tool_result.content 期望字符串。"""
    import json

    return json.dumps(result, ensure_ascii=False)
