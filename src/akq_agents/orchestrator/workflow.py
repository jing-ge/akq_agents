from __future__ import annotations

from pathlib import Path
from pprint import pprint

import yaml

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
    def __init__(self, config, services, store, sqlite_store, reports_dir: Path | None = None):
        self.config = config
        self.services = services
        self.store = store
        self.sqlite_store = sqlite_store
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

    def run_once(self):
        state = self.store.load()
        context = AgentContext(state=state)
        outputs = {}
        for agent in self.agents:
            outputs[agent.name] = agent.run(context)
        self.store.save(context.state)
        self._persist_state(context.state)
        return outputs

    def _persist_state(self, state):
        market_rows = [
            {
                "ts": item["timestamp"],
                "symbol": item["symbol"],
                "close": item["close"],
                "volume": item["volume"],
                "extras_yaml": yaml.safe_dump(item.get("extras", {}), allow_unicode=True, sort_keys=False),
            }
            for item in state.get("market_snapshots", [])
        ]
        factor_rows = [
            {
                "ts": item["timestamp"],
                "symbol": item["symbol"],
                "factor_name": item["factor_name"],
                "value": item["value"],
            }
            for item in state.get("factor_scores", [])
        ]
        backtest_rows = [
            {
                "ts": item["timestamp"],
                "factor_name": item["factor_name"],
                "annual_return": item["annual_return"],
                "sharpe": item["sharpe"],
                "max_drawdown": item["max_drawdown"],
                "win_rate": item["win_rate"],
                "score": item["score"],
            }
            for item in state.get("backtest_reports", [])
        ]
        portfolio_rows = [
            {
                "ts": state.get("daily_advice", {}).get("generated_at", ""),
                "symbol": item["symbol"],
                "weight": item["weight"],
                "score": item["score"],
                "reasons_yaml": yaml.safe_dump(item.get("reasons", []), allow_unicode=True, sort_keys=False),
            }
            for item in state.get("portfolio", [])
        ]
        advice = state.get("daily_advice")
        advice_rows = []
        if advice:
            advice_rows.append(
                {
                    "ts": advice["generated_at"],
                    "rendered": advice["rendered"],
                    "payload_yaml": yaml.safe_dump(advice, allow_unicode=True, sort_keys=False),
                }
            )

        self.sqlite_store.insert_rows("market_snapshots", market_rows)
        self.sqlite_store.insert_rows("factor_scores", factor_rows)
        self.sqlite_store.insert_rows("backtest_reports", backtest_rows)
        self.sqlite_store.insert_rows("portfolio_recommendations", portfolio_rows)
        self.sqlite_store.insert_rows("daily_advices", advice_rows)

    def run_once_and_print(self):
        outputs = self.run_once()
        pprint(outputs["advisor-agent"]["rendered"])
        pprint(outputs["report-agent"]["report_path"])
        return outputs
