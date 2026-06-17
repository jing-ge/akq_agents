from __future__ import annotations

from akq_agents.models.domain import DailyAdvice


class SimpleAdvisorService:
    def render_daily_advice(self, advice: DailyAdvice) -> str:
        return (
            f"生成时间: {advice.generated_at:%Y-%m-%d %H:%M:%S}\n"
            f"总结: {advice.summary}\n"
            f"观察池: {', '.join(advice.watchlist)}\n"
            f"买入候选: {', '.join(advice.buy_candidates)}\n"
            f"减仓候选: {', '.join(advice.reduce_candidates)}\n"
            f"风险提示: {'; '.join(advice.risk_notes)}"
        )
