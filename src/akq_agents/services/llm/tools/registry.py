"""ToolRegistry + ToolSpec：LLM 可调工具注册表。

启动期 **强校验** ``read_only=True``。任何 LLM 可见工具必须只读；写库由 Orchestrator
基础设施层完成（spec v2：见 ``orchestrator.py`` 的边界说明）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    json_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    read_only: Literal[True] = True
    truncate_chars: int = 8000  # 序列化后字节上限，超出截断


def _validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """最小化的 JSON Schema 校验：只覆盖 type/required/properties.type，足以拦截 LLM 常见错误。

    返回 None 表示通过；返回 str 表示错误信息。不引入 jsonschema 第三方依赖
    （Karpathy Simplicity First：当前 4 个工具的 schema 都很浅，自己写够用）。
    """
    if schema.get("type") not in (None, "object"):
        return f"unsupported schema.type={schema.get('type')!r}"
    if not isinstance(args, dict):
        return f"args must be object, got {type(args).__name__}"
    required = schema.get("required", []) or []
    for key in required:
        if key not in args:
            return f"missing required argument {key!r}"
    properties = schema.get("properties", {}) or {}
    for key, spec in properties.items():
        if key not in args:
            continue
        expected_type = spec.get("type")
        if expected_type is None:
            continue
        if not _type_matches(args[key], expected_type):
            return f"argument {key!r} expected {expected_type}, got {type(args[key]).__name__}"
    return None


def _type_matches(value: Any, expected: str) -> bool:
    mapping = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    py_type = mapping.get(expected)
    if py_type is None:
        return True  # 未知类型 → 默认放行
    # bool 是 int 的子类，特殊处理
    if expected == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, py_type)


class ToolRegistry:
    """全局工具注册表。

    用法：
        reg = ToolRegistry()
        reg.register(ToolSpec(name="get_data_health", ..., read_only=True))
        # ChatAgent 调：
        result = reg.invoke("get_data_health", args={}, session_id="...")
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("ToolSpec.name must be non-empty")
        if spec.read_only is not True:
            raise ValueError(f"P4: tool {spec.name!r} must be read_only=True; got {spec.read_only!r}")
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not registered")
        return self._tools[name]

    def list_anthropic_specs(self) -> list[dict[str, Any]]:
        """转换为 Anthropic tools schema。"""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.json_schema,
            }
            for spec in self._tools.values()
        ]

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """安全执行一次工具调用。

        - name 不存在 → ``{"error": "TOOL_NOT_FOUND"}``
        - args schema 校验失败 → ``{"error": "INVALID_ARGUMENTS", "detail": ...}``
        - handler 异常 → ``{"error": "INTERNAL", "message": ...}``（不泄露 traceback）
        - 序列化后超 truncate_chars → 截断 + ``"_truncated": true``
        """
        _ = session_id  # 预留，未来可做按 session 配额
        if name not in self._tools:
            return {"error": "TOOL_NOT_FOUND", "name": name}
        spec = self._tools[name]
        err = _validate_args(spec.json_schema, args)
        if err is not None:
            return {"error": "INVALID_ARGUMENTS", "detail": err}
        try:
            result = spec.handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s execution failed", name)
            return {"error": "INTERNAL", "message": str(exc)[:300]}
        return _truncate(result, max_chars=spec.truncate_chars)


def _truncate(result: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """如序列化 JSON 长度超 max_chars，添加 _truncated 标记。"""
    try:
        s = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"error": "INTERNAL", "message": "result not JSON serializable"}
    if len(s) > max_chars:
        return {"_truncated": True, "summary": s[: max_chars - 100] + "...[truncated]"}
    return result
