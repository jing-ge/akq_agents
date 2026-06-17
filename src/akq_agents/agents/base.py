from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentContext:
    state: dict[str, Any]


class BaseAgent:
    name = "base-agent"

    def run(self, context: AgentContext) -> dict[str, Any]:
        raise NotImplementedError
