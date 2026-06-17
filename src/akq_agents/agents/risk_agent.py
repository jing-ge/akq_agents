from __future__ import annotations

from akq_agents.agents.base import AgentContext, BaseAgent


class RiskAgent(BaseAgent):
    name = "risk-agent"

    def __init__(self, max_single_weight: float, min_single_weight: float, max_portfolio_size: int, min_liquidity_score: float):
        self.max_single_weight = max_single_weight
        self.min_single_weight = min_single_weight
        self.max_portfolio_size = max_portfolio_size
        self.min_liquidity_score = min_liquidity_score

    def run(self, context: AgentContext):
        portfolio = context.state.get("portfolio", [])[: self.max_portfolio_size]
        factor_scores = context.state.get("factor_scores", [])
        liquidity_by_symbol = {}
        for item in factor_scores:
            if item["factor_name"] == "liquidity":
                liquidity_by_symbol[item["symbol"]] = item["value"]

        filtered = []
        risk_notes = []
        for item in portfolio:
            liquidity_score = liquidity_by_symbol.get(item["symbol"], 0.0)
            if liquidity_score < self.min_liquidity_score:
                risk_notes.append(f"{item['symbol']} 流动性不足，已从建议组合剔除")
                continue
            filtered.append(item)

        if not filtered:
            context.state["portfolio"] = []
            context.state["risk_notes"] = risk_notes + ["无满足风控要求的标的"]
            return {"portfolio": [], "risk_notes": context.state["risk_notes"]}

        for item in filtered:
            item["weight"] = min(item["weight"], self.max_single_weight)

        total_weight = sum(item["weight"] for item in filtered)
        normalized = []
        for item in filtered:
            weight = item["weight"] / total_weight if total_weight else 0.0
            if weight < self.min_single_weight:
                risk_notes.append(f"{item['symbol']} 权重低于下限，已降级为观察标的")
                continue
            item["weight"] = weight
            normalized.append(item)

        renormalized_total = sum(item["weight"] for item in normalized)
        if renormalized_total:
            for item in normalized:
                item["weight"] = item["weight"] / renormalized_total

        context.state["portfolio"] = normalized
        context.state["risk_notes"] = risk_notes
        return {"portfolio": normalized, "risk_notes": risk_notes}
