"""P5 Web 配置（``config/web.yaml``）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PollIntervalsConfig(BaseModel):
    ops_health: int = 5000
    ops_jobs: int = 5000
    ops_events: int = 3000


class ChatSSEConfig(BaseModel):
    sse_keepalive_s: int = 15
    max_message_chars: int = 4000


class UIConfig(BaseModel):
    title: str = "AKQ Agents Console"
    timezone: str = "Asia/Shanghai"


class EChartsConfig(BaseModel):
    use_cdn: bool = True
    cdn_url: str = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


class WebConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    poll_intervals_ms: PollIntervalsConfig = Field(default_factory=PollIntervalsConfig)
    chat: ChatSSEConfig = Field(default_factory=ChatSSEConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    echarts: EChartsConfig = Field(default_factory=EChartsConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> WebConfig:
        with open(path, encoding="utf-8") as f:
            payload: dict[str, Any] = yaml.safe_load(f) or {}
        return cls.model_validate(payload.get("web", {}))
