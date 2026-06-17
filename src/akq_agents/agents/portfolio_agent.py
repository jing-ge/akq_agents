from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import PortfolioRecommendation


class PortfolioAgent(BaseAgent):
    name = "portfolio-agent"

    def __init__(self, top_n_symbols: int):
        self.top_n_symbols = top_n_symbols

    def run(self, context: AgentContext):
        selected_factors = {item["factor_name"] for item in context.state.get("selected_factors", [])}
        factor_scores = context.state.get("factor_scores", [])

        total_scores = defaultdict(float)
        reasons = defaultdict(list)
        for item in factor_scores:
            if item["factor_name"] not in selected_factors:
                continue
            total_scores[item["symbol"]] += item["value"]
            reasons[item["symbol"]].append(f"{item['factor_name']}={item['value']:.4f}")

        ranked = sorted(total_scores.items(), key=lambda pair: pair[1], reverse=True)[: self.top_n_symbols]
        total = sum(score for _, score in ranked) or 1.0

        recommendations = [
            PortfolioRecommendation(
                symbol=symbol,
                weight=score / total,
                score=score,
                reasons=reasons[symbol],
            )
            for symbol, score in ranked
        ]
        context.state["portfolio"] = [asdict(item) for item in recommendations]
        return {"portfolio": recommendations}
