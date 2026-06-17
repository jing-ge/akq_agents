from __future__ import annotations

from akq_agents.agents.base import AgentContext, BaseAgent


class ResearchAgent(BaseAgent):
    name = "research-agent"

    def __init__(self, top_n_factors: int, min_sharpe: float, max_drawdown: float, min_ic: float):
        self.top_n_factors = top_n_factors
        self.min_sharpe = min_sharpe
        self.max_drawdown = max_drawdown
        self.min_ic = min_ic

    def run(self, context: AgentContext):
        reports = context.state.get("backtest_reports", [])
        eligible = [
            item
            for item in reports
            if item["sharpe"] >= self.min_sharpe
            and item["max_drawdown"] <= self.max_drawdown
            and item.get("ic", 0.0) >= self.min_ic
        ]
        selected = sorted(eligible, key=lambda item: item["score"], reverse=True)[: self.top_n_factors]
        context.state["selected_factors"] = selected
        context.state["research_summary"] = {
            "eligible_factor_count": len(eligible),
            "selected_factor_count": len(selected),
        }
        return {"selected_factors": selected}
