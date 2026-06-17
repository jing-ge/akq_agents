from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import List

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import MarketSnapshot


class FactorAgent(BaseAgent):
    name = "factor-agent"

    def __init__(self, factor_library):
        self.factor_library = factor_library

    def run(self, context: AgentContext):
        raw_snapshots = context.state.get("market_snapshots", [])
        snapshots: List[MarketSnapshot] = [
            MarketSnapshot(
                symbol=item["symbol"],
                close=item["close"],
                volume=item["volume"],
                timestamp=datetime.fromisoformat(item["timestamp"]) if isinstance(item["timestamp"], str) else item["timestamp"],
                extras=item.get("extras", {}),
            )
            for item in raw_snapshots
        ]

        factor_scores = self.factor_library.compute_factor_scores(snapshots)
        serialized = []
        for item in factor_scores:
            payload = asdict(item)
            payload["timestamp"] = item.timestamp.isoformat()
            serialized.append(payload)
        context.state["factor_scores"] = serialized
        return {"factor_scores": factor_scores}
