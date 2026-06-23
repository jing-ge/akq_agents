from __future__ import annotations

from pathlib import Path
from pprint import pprint

from akq_agents.agents.advisor_agent import AdvisorAgent
from akq_agents.agents.analyst_agent import AnalystAgent
from akq_agents.agents.backtest_agent import BacktestAgent
from akq_agents.agents.base import AgentContext
from akq_agents.agents.data_agent import DataAgent
from akq_agents.agents.factor_agent import FactorAgent
from akq_agents.agents.portfolio_agent import PortfolioAgent
from akq_agents.agents.report_agent import ReportAgent
from akq_agents.agents.research_agent import ResearchAgent
from akq_agents.agents.risk_agent import RiskAgent

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class QuantWorkflow:
    def __init__(self, config, services, store, reports_dir: Path | None = None):
        self.config = config
        self.services = services
        self.store = store
        reports_path = Path(reports_dir) if reports_dir else _PROJECT_ROOT / "reports"
        agents: list = [
            DataAgent(
                services["market"],
                config.universe.symbols,
                config.universe.lookback_days,
                repository=services.get("data_repository"),
            ),
            FactorAgent(services["factor"], repository=services.get("data_repository")),
            BacktestAgent(services["backtest"]),
            ResearchAgent(
                config.research.top_n_factors,
                config.research.min_sharpe,
                config.research.max_drawdown,
                config.research.min_ic,
            ),
            PortfolioAgent(config.research.top_n_symbols, services=services),
            RiskAgent(
                config.risk.max_single_weight,
                config.risk.min_single_weight,
                config.risk.max_portfolio_size,
                config.risk.min_liquidity_score,
            ),
            AdvisorAgent(services["advisor"]),
        ]
        # P4 AnalystAgent：在 ReportAgent 之前；LLM 组件齐时才注入
        if {"llm_orchestrator", "llm_config"}.issubset(services.keys()):
            llm_cfg = services["llm_config"]
            agents.append(
                AnalystAgent(
                    orchestrator=services["llm_orchestrator"],
                    cfg=llm_cfg.analyst,
                    reports_dir=reports_path,
                    safety=llm_cfg.safety,
                )
            )
        agents.append(ReportAgent(str(reports_path)))
        self.agents = agents

    def run_once(self, *, recorder=None):
        """跑一次完整链路。可选传入 StepRecorder 把每个 agent 步骤落到 job_steps 表。"""
        state = self.store.load()
        context = AgentContext(state=state)
        outputs = {}
        for agent in self.agents:
            if recorder is not None:
                with recorder.step(agent.name) as step_ctx:
                    out = agent.run(context)
                    outputs[agent.name] = out
                    # 提取 agent 输出的简要摘要写到 step payload
                    step_ctx.set_payload(_summarize_agent_output(agent.name, out, context))
            else:
                outputs[agent.name] = agent.run(context)
        self.store.save(context.state)
        return outputs

    def run_once_and_print(self):
        outputs = self.run_once()
        pprint(outputs["advisor-agent"]["rendered"])
        pprint(outputs["report-agent"]["report_path"])
        return outputs


def _summarize_agent_output(name: str, output, context: AgentContext) -> dict:
    """把 agent.run 的返回 + context 状态压缩成可读的 step payload。"""
    payload: dict = {"agent": name}
    if isinstance(output, dict):
        for k, v in output.items():
            # 数值 / 字符串直接放；复杂结构只放长度信息
            if isinstance(v, (int, float, bool, str)) or v is None:
                payload[k] = v
            elif isinstance(v, list):
                payload[f"{k}_n"] = len(v)
                if v and isinstance(v[0], dict):
                    payload[f"{k}_sample"] = v[0]  # 只保 1 个样本
            elif isinstance(v, dict):
                payload[f"{k}_keys"] = list(v.keys())[:10]

    # 补充关键 context.state 信息
    state = context.state
    if name == "data-agent":
        snaps = state.get("market_snapshots") or []
        payload["snapshots_count"] = len(snaps)
        payload["data_source"] = state.get("data_agent_status", "?")
    elif name == "factor-agent":
        scores = state.get("factor_scores") or []
        payload["factor_scores_count"] = len(scores)
    elif name == "portfolio-agent":
        port = state.get("portfolio") or []
        payload["portfolio_size"] = len(port)
        payload["turnover"] = state.get("portfolio_turnover")
        payload["risk_filter_excluded"] = state.get("risk_filter_excluded")
    elif name == "advisor-agent":
        advice = state.get("daily_advice") or {}
        payload["buy"] = advice.get("buy_candidates", [])[:5]
        payload["reduce"] = advice.get("reduce_candidates", [])[:5]
        payload["risk_notes_count"] = len(advice.get("risk_notes", []))
    elif name == "report-agent":
        payload["report_path"] = (output or {}).get("report_path") if isinstance(output, dict) else None
    return payload
