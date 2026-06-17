from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class AgentContext:
    state: Dict[str, Any]


class BaseAgent:
    name = "base-agent"

    def run(self, context: AgentContext) -> Dict[str, Any]:
        raise NotImplementedError
