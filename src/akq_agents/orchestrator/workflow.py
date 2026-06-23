from __future__ import annotations

from pathlib import Path
from pprint import pprint

from akq_agents.agents.analyst_agent import AnalystAgent
from akq_agents.agents.base import AgentContext
from akq_agents.agents.portfolio_agent import PortfolioAgent

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class QuantWorkflow:
    def __init__(self, config, services, reports_dir: Path | None = None):
        self.config = config
        self.services = services
        reports_path = Path(reports_dir) if reports_dir else _PROJECT_ROOT / "reports"
        analyst = None
        if {"llm_orchestrator", "llm_config"}.issubset(services.keys()):
            llm_cfg = services["llm_config"]
            analyst = AnalystAgent(
                orchestrator=services["llm_orchestrator"],
                cfg=llm_cfg.analyst,
                reports_dir=reports_path,
                safety=llm_cfg.safety,
            )
        self.agents = [
            agent
            for agent in [
                PortfolioAgent(config.research.top_n_symbols, services=services),
                analyst,
            ]
            if agent is not None
        ]

    def run_once(self, *, recorder=None):
        """跑一次完整链路。可选传入 StepRecorder 把每个 agent 步骤落到 job_steps 表。

        agents 之间通过单次 run 内的 context 直接传递（不再持久化到 yaml）。
        PortfolioAgent 写 portfolio/attribution/portfolio_turnover；AnalystAgent 读它们。
        """
        context = AgentContext(state={})
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
        return outputs

    def run_once_and_print(self):
        outputs = self.run_once()
        pprint(outputs.get("analyst-agent", {}).get("rendered", "(no analyst)"))
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
    if name == "portfolio-agent":
        port = state.get("portfolio") or []
        payload["portfolio_size"] = len(port)
        payload["turnover"] = state.get("portfolio_turnover")
        payload["risk_filter_excluded"] = state.get("risk_filter_excluded")
    return payload
