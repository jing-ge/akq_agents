"""LLMStore：``llm_calls`` + ``chat_messages`` 表读写。

复用 P1 ``meta.db`` 与 WAL 契约。基础设施层 — 不受 ``read_only`` 约束。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_LLM_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  agent TEXT NOT NULL,
  session_id TEXT,
  model TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER,
  tool_calls INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  reason_code TEXT,
  error_msg TEXT
);
"""

_LLM_CALLS_INDEX = "CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);"

_CHAT_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tool_name TEXT,
  tool_args TEXT,
  tool_result TEXT,
  tokens INTEGER
);
"""

_CHAT_MESSAGES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_sid_ts ON chat_messages(session_id, ts);"
)


@dataclass
class LLMCall:
    id: int
    ts: str
    agent: str
    session_id: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    tool_calls: int
    status: str
    reason_code: str | None
    error_msg: str | None


@dataclass
class ChatMessage:
    id: int
    session_id: str
    ts: str
    role: str
    content: str
    tool_name: str | None
    tool_args: str | None
    tool_result: str | None
    tokens: int | None


class LLMStore:
    """llm_calls + chat_messages 表读写封装。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_LLM_CALLS_SCHEMA)
            conn.execute(_LLM_CALLS_INDEX)
            conn.execute(_CHAT_MESSAGES_SCHEMA)
            conn.execute(_CHAT_MESSAGES_INDEX)
            conn.commit()

    # ---- llm_calls ----

    def insert_call(
        self,
        *,
        agent: str,
        session_id: str | None,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int | None = None,
        tool_calls: int = 0,
        status: str = "ok",
        reason_code: str | None = None,
        error_msg: str | None = None,
    ) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (ts, agent, session_id, model, prompt_tokens, completion_tokens,
                                       latency_ms, tool_calls, status, reason_code, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    agent,
                    session_id,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    tool_calls,
                    status,
                    reason_code,
                    error_msg,
                ),
            )
            conn.commit()

    def list_calls(self, *, limit: int = 20, agent: str | None = None) -> list[LLMCall]:
        sql = (
            "SELECT id, ts, agent, session_id, model, prompt_tokens, completion_tokens, "
            "latency_ms, tool_calls, status, reason_code, error_msg "
            "FROM llm_calls WHERE 1=1"
        )
        params: list[Any] = []
        if agent is not None:
            sql += " AND agent = ?"
            params.append(agent)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with open_meta_db(self._db) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [LLMCall(*r) for r in rows]

    # ---- chat_messages ----

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: dict[str, Any] | None = None,
        tokens: int | None = None,
    ) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (session_id, ts, role, content,
                                           tool_name, tool_args, tool_result, tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    datetime.now().isoformat(),
                    role,
                    content,
                    tool_name,
                    json.dumps(tool_args, ensure_ascii=False) if tool_args is not None else None,
                    json.dumps(tool_result, ensure_ascii=False) if tool_result is not None else None,
                    tokens,
                ),
            )
            conn.commit()

    def list_messages(self, session_id: str, *, limit: int = 200) -> list[ChatMessage]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, ts, role, content, tool_name, tool_args, tool_result, tokens
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [ChatMessage(*r) for r in rows]

    def recent_messages(self, session_id: str, *, limit: int = 20) -> list[ChatMessage]:
        """最近 N 条（DESC 取出后反转，得到时间升序）。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, ts, role, content, tool_name, tool_args, tool_result, tokens
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [ChatMessage(*r) for r in reversed(rows)]

    def list_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """按 session_id 聚合返回最近会话（last_ts / message_count）。"""
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT session_id, MAX(ts) AS last_ts, MIN(ts) AS first_ts, COUNT(*) AS n
                FROM chat_messages
                GROUP BY session_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {"session_id": r[0], "last_ts": r[1], "first_ts": r[2], "message_count": r[3]}
            for r in rows
        ]
