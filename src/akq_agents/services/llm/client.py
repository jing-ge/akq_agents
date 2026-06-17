"""LLMClient 协议 + GatewayLLMClient（本地代理 Anthropic 协议）。

只支持 Anthropic Messages API（``POST {base_url}/anthropic/v1/messages``）。OpenAI
fallback 已砍（spec v2 收敛）；如真需要再加。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


# -------- domain types --------


@dataclass
class ToolUseRequest:
    """LLM 提出的一次工具调用。"""

    id: str  # tool_use_id
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """LLMClient.chat 的标准返回。"""

    text: str
    tool_uses: list[ToolUseRequest] = field(default_factory=list)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "error"] = "end_turn"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] | None = None


# -------- exceptions --------


class LLMGatewayError(RuntimeError):
    """LLM 网关层异常。"""

    def __init__(self, message: str, *, reason_code: str = "UPSTREAM_ERROR"):
        super().__init__(message)
        self.reason_code = reason_code


# -------- protocol --------


class LLMClient(Protocol):
    """统一 LLM 客户端协议。"""

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],  # Anthropic Messages 格式 [{role, content}]
        tools: list[dict[str, Any]] | None = None,  # Anthropic tools schema
        max_tokens: int,
        temperature: float,
        timeout_s: int,
    ) -> LLMResponse:
        ...


# -------- gateway client --------


@dataclass
class LLMGatewayConfig:
    base_url: str = "http://127.0.0.1:18931"
    anthropic_path: str = "/anthropic/v1/messages"
    timeout_s: int = 60
    max_retries: int = 2


class GatewayLLMClient:
    """走本地代理的 Anthropic-protocol LLM 客户端。

    路由：``POST {base_url}{anthropic_path}``。
    """

    def __init__(self, cfg: LLMGatewayConfig | None = None) -> None:
        self._cfg = cfg or LLMGatewayConfig()

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        temperature: float,
        timeout_s: int | None = None,
    ) -> LLMResponse:
        timeout = timeout_s if timeout_s is not None else self._cfg.timeout_s
        payload: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        url = self._cfg.base_url.rstrip("/") + self._cfg.anthropic_path
        last_error: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                data = self._post_json(url, payload, timeout=timeout)
            except _RateLimited as exc:
                last_error = exc
                if attempt < self._cfg.max_retries:
                    time.sleep(1.0 * (2**attempt))
                    continue
                raise LLMGatewayError("rate limited", reason_code="RATE_LIMITED") from exc
            except _UpstreamError as exc:
                last_error = exc
                if attempt < self._cfg.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise LLMGatewayError(str(exc), reason_code="UPSTREAM_ERROR") from exc
            except _TimeoutError as exc:
                last_error = exc
                if attempt < self._cfg.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise LLMGatewayError(f"timeout after {attempt + 1} attempts", reason_code="TIMEOUT") from exc
            except _ClientError as exc:
                # 4xx 不重试
                raise LLMGatewayError(str(exc), reason_code="UPSTREAM_ERROR") from exc

            return self._parse_anthropic_response(data)

        raise LLMGatewayError(f"unreachable: {last_error!r}")

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """统一 POST + JSON 解析；分类异常给上层重试。"""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            if exc.code == 429:
                raise _RateLimited(text) from exc
            if exc.code >= 500:
                raise _UpstreamError(f"upstream {exc.code}: {text[:200]}") from exc
            raise _ClientError(f"client error {exc.code}: {text[:200]}") from exc
        except urllib.error.URLError as exc:
            # 网络层异常（含 timeout）
            reason = exc.reason
            if isinstance(reason, TimeoutError) or (hasattr(reason, "__class__") and "timeout" in type(reason).__name__.lower()):
                raise _TimeoutError(str(reason)) from exc
            raise _UpstreamError(f"network: {reason!r}") from exc
        except TimeoutError as exc:
            raise _TimeoutError(str(exc)) from exc

        if status >= 500:
            raise _UpstreamError(f"upstream {status}: {text[:200]}")
        if status == 429:
            raise _RateLimited(text)
        if status >= 400:
            raise _ClientError(f"client error {status}: {text[:200]}")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise _UpstreamError(f"invalid json: {exc!r}; body={text[:200]}") from exc

    @staticmethod
    def _parse_anthropic_response(data: dict[str, Any]) -> LLMResponse:
        """解析 Anthropic Messages API 返回。"""
        content = data.get("content", [])
        if not isinstance(content, list):
            content = []

        text_parts = []
        tool_uses: list[ToolUseRequest] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_uses.append(
                    ToolUseRequest(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        input=dict(block.get("input", {})),
                    )
                )

        stop_reason = data.get("stop_reason", "end_turn")
        if stop_reason not in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
            stop_reason = "end_turn"

        usage = data.get("usage", {}) or {}
        return LLMResponse(
            text="".join(text_parts),
            tool_uses=tool_uses,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            prompt_tokens=int(usage.get("input_tokens", 0) or 0),
            completion_tokens=int(usage.get("output_tokens", 0) or 0),
            raw=data,
        )


# 内部异常分类（不暴露给外层）
class _RateLimited(Exception):
    pass


class _UpstreamError(Exception):
    pass


class _ClientError(Exception):
    pass


class _TimeoutError(Exception):
    pass
