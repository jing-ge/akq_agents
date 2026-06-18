"""Chat endpoints：sessions + SSE message stream。"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


class CreateSessionRequest(BaseModel):
    pass


class SendMessageRequest(BaseModel):
    content: str
    model: str | None = None


@router.get("/sessions")
async def list_sessions(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.llm_store is None:
        return {"sessions": [], "n": 0}
    sessions = svc.llm_store.list_sessions(limit=limit)
    return {"sessions": sessions, "n": len(sessions)}


@router.post("/sessions")
async def create_session() -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.llm_store is None or svc.llm_config is None:
        raise HTTPException(503, detail="llm not configured")
    session_id = f"chat:{uuid.uuid4().hex[:8]}"
    # 写一个 system 消息作为 session 起点
    from akq_agents.agents.chat_agent import _load_system_prompt  # type: ignore[attr-defined]

    system_prompt = _load_system_prompt() + "\n\n" + svc.llm_config.safety.disclaimer_header
    svc.llm_store.append_message(session_id=session_id, role="system", content=system_prompt)
    return {"session_id": session_id}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    """删除一个会话（chat_messages 全部记录 + llm_calls 关联记录）。"""
    svc: ServiceContainer = get_services()
    if svc.llm_store is None:
        raise HTTPException(503, detail="llm not configured")
    from akq_agents.services.data.repository import open_meta_db

    db_path = svc.repo._base_dir / "meta.db" if svc.repo else None
    if db_path is None:
        raise HTTPException(503, detail="repo not ready")
    with open_meta_db(db_path) as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM llm_calls WHERE session_id = ?", (session_id,))
        conn.commit()
    return {"status": "ok", "session_id": session_id}


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.llm_store is None:
        raise HTTPException(503, detail="llm not configured")
    msgs = svc.llm_store.list_messages(session_id, limit=limit)
    return {
        "session_id": session_id,
        "messages": [
            {
                "id": m.id,
                "ts": m.ts,
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "tool_args": json.loads(m.tool_args) if m.tool_args else None,
                "tool_result": json.loads(m.tool_result) if m.tool_result else None,
            }
            for m in msgs
        ],
        "n": len(msgs),
    }


@router.post("/sessions/{session_id}/messages")
async def post_message(session_id: str, body: SendMessageRequest) -> StreamingResponse:
    """SSE：调 LLMOrchestrator.run_chat_turn（非流式），完成后 send 一次性事件。

    Anthropic 协议下 P4 已确认不实现 streaming（spec 附录 B §4）；
    P5 兑现同一承诺：等完整响应再 send 一次 SSE done。
    """
    svc: ServiceContainer = get_services()
    if svc.llm_store is None or svc.llm_orchestrator is None or svc.llm_config is None:
        raise HTTPException(503, detail="llm not configured")
    if not body.content.strip():
        raise HTTPException(400, detail="content required")
    if len(body.content) > svc.web_config.chat.max_message_chars if svc.web_config else 4000:
        raise HTTPException(400, detail="content too long")

    chat_cfg = svc.llm_config.chat
    model = body.model or chat_cfg.model
    history = _build_history(svc, session_id, limit=chat_cfg.history_window)

    async def event_stream():
        # keepalive ping
        yield ": connected\n\n"
        # 非流式：在 worker 跑 run_chat_turn；结束后发送结果
        loop = asyncio.get_running_loop()
        from akq_agents.services.llm.client import LLMGatewayError

        try:
            text = await loop.run_in_executor(
                None,
                _run_chat_turn_sync,
                svc, session_id, history, body.content, model,
            )
        except LLMGatewayError as exc:
            payload = {"reason_code": exc.reason_code, "message": str(exc)[:200]}
            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
            return

        # 抓 LLM 本轮新写入的工具调用消息（role='tool'）
        if svc.llm_store is not None:
            tool_msgs = [m for m in svc.llm_store.list_messages(session_id, limit=200) if m.role == "tool"]
            # 仅本轮的：tool_msgs[-N:]（粗略：发送所有 tool 调用让前端自决）
            recent_tools = tool_msgs[-chat_cfg.max_iterations:] if tool_msgs else []
            for tm in recent_tools:
                yield f"event: tool_use\ndata: {json.dumps({'name': tm.tool_name, 'args': json.loads(tm.tool_args) if tm.tool_args else None, 'result': json.loads(tm.tool_result) if tm.tool_result else None}, ensure_ascii=False)}\n\n"

        yield f"event: assistant\ndata: {json.dumps({'content': text}, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _build_history(svc: ServiceContainer, session_id: str, limit: int) -> list[dict[str, Any]]:
    msgs = svc.llm_store.recent_messages(session_id, limit=limit) if svc.llm_store else []
    out: list[dict[str, Any]] = []
    for m in msgs:
        if m.role not in {"user", "assistant"} or not m.content:
            continue
        out.append({"role": m.role, "content": m.content})
    return out


def _run_chat_turn_sync(
    svc: ServiceContainer,
    session_id: str,
    history: list[dict[str, Any]],
    user_message: str,
    model: str,
) -> str:
    """运行在线程池：调 P4 LLMOrchestrator 的同步 run_chat_turn。"""
    assert svc.llm_orchestrator is not None and svc.llm_config is not None
    chat_cfg = svc.llm_config.chat

    # system prompt：从 chat_messages 的 system 行取
    system_prompt = ""
    if svc.llm_store is not None:
        all_msgs = svc.llm_store.list_messages(session_id, limit=10)
        for m in all_msgs:
            if m.role == "system":
                system_prompt = m.content
                break

    return svc.llm_orchestrator.run_chat_turn(
        session_id=session_id,
        system_prompt=system_prompt or svc.llm_config.safety.disclaimer_header,
        history=history,
        user_message=user_message,
        model=model,
        max_tokens=chat_cfg.max_tokens,
        temperature=chat_cfg.temperature,
    )
