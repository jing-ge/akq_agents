from __future__ import annotations

from dataclasses import asdict

from akq_agents.agents.base import AgentContext, BaseAgent


class DataAgent(BaseAgent):
    name = "data-agent"

    def __init__(self, market_service, symbols, lookback_days: int):
        self.market_service = market_service
        self.symbols = symbols
        self.lookback_days = lookback_days

    def run(self, context: AgentContext):
        snapshots = self.market_service.fetch_market_snapshots(self.symbols, self.lookback_days)
        serialized = []
        for item in snapshots:
            payload = asdict(item)
            payload["timestamp"] = item.timestamp.isoformat()
            serialized.append(payload)
        context.state["market_snapshots"] = serialized
        return {"snapshots": snapshots}
