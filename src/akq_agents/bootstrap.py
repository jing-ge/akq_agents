"""应用启动装配器。

负责把 ``config/system.yaml`` 和 ``config/data.yaml`` 加载成 pydantic 配置，
并装配 services / agents / workflow。

P1 改造点：新增 :class:`DataRepository` 装配（含 gateway / calendar / universe /
quality_gate），可被 FactorAgent 等通过 ``services['data_repository']`` 引用。
"""

from __future__ import annotations

from pathlib import Path

from akq_agents.models.config import AppConfig
from akq_agents.models.data_config import DataConfig
from akq_agents.orchestrator.workflow import QuantWorkflow
from akq_agents.services.akshare_service import AkshareService, MockAkshareService
from akq_agents.services.backtest_service import AkquantBacktestService, MockBacktestService
from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.calendar import TradingCalendar
from akq_agents.services.data.quality import QualityGate
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.data.universe import UniverseManager
from akq_agents.services.factor_service import FactorLibrary
from akq_agents.services.llm_service import SimpleAdvisorService
from akq_agents.services.storage import SQLiteStore, StateStore

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "system.yaml"
DATA_CONFIG_PATH = BASE_DIR / "config" / "data.yaml"


def build_services(config: AppConfig, data_config: DataConfig | None = None) -> dict[str, object]:
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

    services: dict[str, object] = {
        "market": market_service,
        "backtest": backtest_service,
        "advisor": advisor_service,
        "factor": factor_library,
    }

    # P1：装配数据层 Repository（可选；data.yaml 缺失则跳过，保持旧链路兼容）
    if data_config is not None:
        services["data_repository"] = build_data_repository(data_config)

    return services


def build_data_repository(data_config: DataConfig, project_root: Path = BASE_DIR) -> DataRepository:
    """装配 P1 数据层 :class:`DataRepository` 及其依赖。"""
    gateway = AKShareGateway(data_config.akshare)
    calendar = TradingCalendar()
    universe_manager = UniverseManager(gateway=gateway, config=data_config.universe)
    quality_gate = QualityGate(data_config.quality)
    base_dir = data_config.resolve_base_dir(project_root)
    return DataRepository(
        config=data_config,
        gateway=gateway,
        calendar=calendar,
        universe_manager=universe_manager,
        quality_gate=quality_gate,
        base_dir=base_dir,
    )


def load_data_config(path: Path = DATA_CONFIG_PATH) -> DataConfig | None:
    if not path.exists():
        return None
    return DataConfig.from_yaml(path)


def build_workflow(config_path: Path = CONFIG_PATH):
    config = AppConfig.from_yaml(config_path)
    data_config = load_data_config()
    state_store = StateStore(config.storage.state_file)
    sqlite_store = SQLiteStore(config.storage.sqlite_path)
    services = build_services(config, data_config=data_config)
    return QuantWorkflow(config, services, state_store, sqlite_store), config
