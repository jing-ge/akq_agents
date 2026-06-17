from __future__ import annotations

from datetime import datetime

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import DailyAdvice


class AdvisorAgent(BaseAgent):
    name = "advisor-agent"

    def __init__(self, advisor_service):
        self.advisor_service = advisor_service

    def run(self, context: AgentContext):
        portfolio = context.state.get("portfolio", [])
        watchlist = [item["symbol"] for item in portfolio]
        buy_candidates = watchlist[:2]
        reduce_candidates = watchlist[-1:] if len(watchlist) > 2 else []
        risk_notes = list(context.state.get("risk_notes", []))
        risk_notes.extend(
            [
                "市场风格切换可能导致因子短期失效",
                "实盘需加入滑点、手续费与流动性约束",
            ]
        )

        advice = DailyAdvice(
            generated_at=datetime.now(),
            summary="当前建议以高评分因子主导的强势标的为重点观察对象，优先保留通过风控筛选的候选，避免权重过度集中。",
            watchlist=watchlist,
            buy_candidates=buy_candidates,
            reduce_candidates=reduce_candidates,
            risk_notes=risk_notes,
        )
        rendered = self.advisor_service.render_daily_advice(advice)
        context.state["daily_advice"] = {
            "generated_at": advice.generated_at.isoformat(),
            "summary": advice.summary,
            "watchlist": advice.watchlist,
            "buy_candidates": advice.buy_candidates,
            "reduce_candidates": advice.reduce_candidates,
            "risk_notes": advice.risk_notes,
            "rendered": rendered,
        }
        return {"daily_advice": advice, "rendered": rendered}
