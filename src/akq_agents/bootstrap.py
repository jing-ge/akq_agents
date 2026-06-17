from __future__ import annotations

from pathlib import Path

from akq_agents.models.config import AppConfig
from akq_agents.orchestrator.workflow import QuantWorkflow
from akq_agents.services.akshare_service import AkshareService, MockAkshareService
from akq_agents.services.backtest_service import AkquantBacktestService, MockBacktestService
from akq_agents.services.factor_service import FactorLibrary
from akq_agents.services.llm_service import SimpleAdvisorService
from akq_agents.services.storage import SQLiteStore, StateStore


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "system.yaml"


def build_services(config: AppConfig):
    if config.services.use_mock_data:
        market_service = MockAkshareService()
    else:
        market_service = AkshareService()

    if config.services.use_mock_backtest:
        backtest_service = MockBacktestService(
            commission=config.backtest.commission,
            slippage=config.backtest.slippage,
            initial_capital=config.backtest.initial_capital,
        )
    else:
        backtest_service = AkquantBacktestService(
            benchmark=config.research.benchmark,
            rebalance_frequency=config.research.rebalance_frequency,
            commission=config.backtest.commission,
            slippage=config.backtest.slippage,
            initial_capital=config.backtest.initial_capital,
            start_date=config.backtest.start_date,
            end_date=config.backtest.end_date,
            strict=config.services.strict_real_services,
        )
    advisor_service = SimpleAdvisorService()
    factor_library = FactorLibrary()
    return {
        "market": market_service,
        "backtest": backtest_service,
        "advisor": advisor_service,
        "factor": factor_library,
    }


def build_workflow(config_path: Path = CONFIG_PATH):
    config = AppConfig.from_yaml(config_path)
    state_store = StateStore(config.storage.state_file)
    sqlite_store = SQLiteStore(config.storage.sqlite_path)
    services = build_services(config)
    return QuantWorkflow(config, services, state_store, sqlite_store), config
