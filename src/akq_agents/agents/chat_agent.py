"""ChatAgent CLI REPL（P4）：交互式带 ToolUse loop。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from akq_agents.models.llm_config import ChatSubConfig, SafetyConfig
from akq_agents.services.llm.client import LLMGatewayError
from akq_agents.services.llm.orchestrator import LLMOrchestrator
from akq_agents.services.llm.store import LLMStore

logger = logging.getLogger(__name__)


_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "chat_system.md"


class ChatAgent:
    """ChatAgent CLI REPL。"""

    def __init__(
        self,
        orchestrator: LLMOrchestrator,
        cfg: ChatSubConfig,
        safety: SafetyConfig,
        store: LLMStore,
        event_writer: Any | None = None,
    ) -> None:
        self._orch = orchestrator
        self._cfg = cfg
        self._safety = safety
        self._store = store
        self._event_writer = event_writer  # 可选：spec §3 流程 2 写 chat.session.created

    def repl(self) -> None:
        if not self._cfg.enabled:
            print("chat 已在配置中禁用 (llm.chat.enabled=false)")
            return
        session_id = f"chat:{uuid.uuid4().hex[:8]}"
        system = _load_system_prompt() + "\n\n" + self._safety.disclaimer_header
        # 写一条 system message 作为 session 起点
        self._store.append_message(session_id=session_id, role="system", content=system)
        if self._event_writer is not None:
            try:
                self._event_writer(
                    level="info",
                    kind="chat.session.created",
                    source="chat",
                    payload={"session_id": session_id},
                )
            except Exception:  # noqa: BLE001
                logger.warning("chat.session.created event write failed (ignored)")
        self._banner(session_id)
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input in {"/quit", "/exit"}:
                break
            if user_input.startswith("/help"):
                self._help()
                continue
            history = self._build_history(session_id)
            try:
                text = self._orch.run_chat_turn(
                    session_id=session_id,
                    system_prompt=system,
                    history=history,
                    user_message=user_input,
                    model=self._cfg.model,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                )
            except LLMGatewayError as exc:
                print(f"[LLM 网关错误: {exc.reason_code}] {exc}")
                continue
            print(text)
        print(f"\nchat session ended: {session_id}")

    # ----------------- helpers -----------------

    def _build_history(self, session_id: str) -> list[dict[str, Any]]:
        """从 chat_messages 取最近 history_window 条，转 Anthropic messages 格式。

        过滤策略：仅取 user/assistant 文本（不带 tool_use blocks，避免复杂状态恢复）。
        系统 prompt 不进 history（由 orchestrator 单独传）。
        """
        msgs = self._store.recent_messages(session_id, limit=self._cfg.history_window)
        out: list[dict[str, Any]] = []
        for m in msgs:
            if m.role not in {"user", "assistant"}:
                continue
            if not m.content:
                continue
            out.append({"role": m.role, "content": m.content})
        return out

    def _banner(self, session_id: str) -> None:
        print(
            f"akq-agents chat (session={session_id}, model={self._cfg.model})\n"
            f"输入 /quit 退出，/help 查看命令\n"
            f"{self._safety.disclaimer_header}\n"
        )

    def _help(self) -> None:
        print("命令：/quit 退出 | /help 帮助\n可用工具：get_data_health / list_factors / get_portfolio_snapshot / query_events")


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")
