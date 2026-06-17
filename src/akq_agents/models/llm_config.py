"""P4 LLM 层配置（``config/llm.yaml``）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class GatewayConfig(BaseModel):
    base_url: str = "http://127.0.0.1:18931"
    anthropic_path: str = "/anthropic/v1/messages"
    timeout_s: int = 60
    max_retries: int = 2


class SafetyConfig(BaseModel):
    disclaimer_header: str = "本报告由 LLM 生成，仅供研究参考，不构成投资建议；系统不执行任何交易指令。"


class AnalystSubConfig(BaseModel):
    enabled: bool = True
    model: str = "Claude-Opus-4.7"
    max_tokens: int = 4000
    temperature: float = 0.2
    context_top_holdings: int = 20
    context_events_count: int = 10


class ChatSubConfig(BaseModel):
    enabled: bool = True
    model: str = "Claude-Opus-4.7"
    max_tokens: int = 2000
    temperature: float = 0.4
    max_iterations: int = 6
    history_window: int = 20


class LLMConfig(BaseModel):
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    default_model: str = "Claude-Opus-4.7"
    analyst: AnalystSubConfig = Field(default_factory=AnalystSubConfig)
    chat: ChatSubConfig = Field(default_factory=ChatSubConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> LLMConfig:
        with open(path, encoding="utf-8") as f:
            payload: dict[str, Any] = yaml.safe_load(f) or {}
        return cls.model_validate(payload.get("llm", {}))
