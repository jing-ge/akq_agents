from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import FactorScore


class BacktestAgent(BaseAgent):
    name = "backtest-agent"

    def __init__(self, backtest_service):
        self.backtest_service = backtest_service

    def run(self, context: AgentContext):
        factor_scores = [
            FactorScore(
                symbol=item["symbol"],
                factor_name=item["factor_name"],
                value=item["value"],
                timestamp=datetime.fromisoformat(item["timestamp"]),
            )
            for item in context.state.get("factor_scores", [])
        ]
        reports = self.backtest_service.run_factor_backtests(factor_scores)
        serialized = []
        for item in reports:
            payload = asdict(item)
            payload["timestamp"] = item.timestamp.isoformat()
            serialized.append(payload)
        context.state["backtest_reports"] = serialized
        return {"reports": reports}
