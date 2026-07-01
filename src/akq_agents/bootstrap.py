"""应用启动装配器。

负责把 ``config/system.yaml`` 和 ``config/data.yaml`` 加载成 pydantic 配置，
并装配 services / agents / workflow。

P1 改造点：新增 :class:`DataRepository` 装配（含 gateway / calendar / universe /
quality_gate），可被 FactorAgent 等通过 ``services['data_repository']`` 引用。

P2 改造点：新增 :func:`build_daemon`，装配 :class:`QuantDaemon` + ``meta.db``
依赖（``SchedulerStateStore``、``DaemonStateFile``、``RetryWorker``）。
"""

from __future__ import annotations

from pathlib import Path

from akq_agents.models.config import AppConfig
from akq_agents.models.data_config import DataConfig
from akq_agents.models.llm_config import LLMConfig
from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.models.web_config import WebConfig
from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
from akq_agents.orchestrator.scheduler import QuantDaemon
from akq_agents.orchestrator.state_store import SchedulerStateStore
from akq_agents.orchestrator.workflow import QuantWorkflow
from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.calendar import TradingCalendar
from akq_agents.services.data.quality import QualityGate
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.data.retry_worker import RetryWorker
from akq_agents.services.data.universe import UniverseManager
from akq_agents.services.factors import FactorEngine, build_default_registry
from akq_agents.services.factors.discovery import DiscoveryEngine, restore_accepted_factors
from akq_agents.services.factors.proposal_store import FactorProposalStore
from akq_agents.services.llm import (
    GatewayLLMClient,
    LLMGatewayConfig,
    LLMOrchestrator,
    LLMStore,
    ToolRegistry,
    register_default_tools,
)
from akq_agents.services.portfolio import (
    Attributor,
    CompositeScorer,
    FactorEvaluator,
    OptimizerConfig,
    PortfolioOptimizer,
    PortfolioSnapshotStore,
    Preprocessor,
)
from akq_agents.services.portfolio.backtester import BacktestConfig, PortfolioBacktester
from akq_agents.services.portfolio.industry_map import IndustryMapStore
from akq_agents.services.data.stock_names import StockNameStore
from akq_agents.services.portfolio.paper_trading import PaperTradingConfig, PaperTradingStore
from akq_agents.services.portfolio.risk_filter import RiskFilter, RiskFilterConfig
from akq_agents.services.portfolio.trade_list import HoldingsStore, TradeListStore, TradeListConfig

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "system.yaml"
DATA_CONFIG_PATH = BASE_DIR / "config" / "data.yaml"
SCHEDULER_CONFIG_PATH = BASE_DIR / "config" / "scheduler.yaml"
LLM_CONFIG_PATH = BASE_DIR / "config" / "llm.yaml"
WEB_CONFIG_PATH = BASE_DIR / "config" / "web.yaml"


def build_services(config: AppConfig, data_config: DataConfig | None = None) -> dict[str, object]:
    services: dict[str, object] = {}

    # P1：装配数据层 Repository（可选；data.yaml 缺失则跳过，保持旧链路兼容）
    if data_config is not None:
        repo = build_data_repository(data_config)
        services["data_repository"] = repo
        # P3 portfolio pipeline 组件（仅 data 层就绪时注入）
        base_dir = repo._base_dir
        meta_db_path = base_dir / "meta.db"
        registry = build_default_registry()
        services["factor_registry"] = registry
        services["factor_engine"] = FactorEngine()
        evaluator = FactorEvaluator(meta_db_path=meta_db_path, window=90)
        registry.attach_evaluator(evaluator)
        services["factor_evaluator"] = evaluator
        services["preprocessor"] = Preprocessor()
        # M19: min_abs_ir=0.10 — 因子最近 EWMA |IR| < 0.10 不进组合 (公平筛选,
        # builtin/accepted/shadow 同标准). 调整: 改 config/system.yaml 暴露此参数。
        services["composite_scorer"] = CompositeScorer(
            weighting="ir", evaluator=evaluator, min_abs_ir=0.10,
        )
        services["portfolio_optimizer"] = PortfolioOptimizer(
            OptimizerConfig(top_n=50, max_single_weight=0.05, turnover_aversion=0.7, max_industry_weight=0.30)
        )
        services["attributor"] = Attributor()
        services["portfolio_snapshot_store"] = PortfolioSnapshotStore(meta_db_path)
        # M9-C 行业映射 store
        services["industry_map_store"] = IndustryMapStore(meta_db_path)
        # 股票代码 → 中文简称（UI 展示用）
        services["stock_name_store"] = StockNameStore(meta_db_path)
        # P0-2: Paper Trading 前向跟踪
        services["paper_trading_store"] = PaperTradingStore(
            meta_db_path,
            PaperTradingConfig(assumed_capital=100_000.0),
        )
        # P0-1: Holdings + Trade List（把权重翻译成具体下单清单）
        services["holdings_store"] = HoldingsStore(meta_db_path)
        services["trade_list_store"] = TradeListStore(meta_db_path)
        services["trade_list_config"] = TradeListConfig(assumed_capital=100_000.0)
        # M7-B: 硬风控过滤
        services["risk_filter"] = RiskFilter(RiskFilterConfig(
            min_listing_days=60,
            min_avg_amount=5e7,
            min_price=1.0,
            max_price=1000.0,
        ))
        # M7-A: 组合净值回测器（共享同一 meta.db + parquet 缓存）
        services["portfolio_backtester"] = PortfolioBacktester(
            meta_db_path=meta_db_path,
            ohlcv_dir=repo._ohlcv_dir,
            cfg=BacktestConfig(
                commission=config.backtest.commission,
                slippage=config.backtest.slippage,
            ),
        )

        # P2 scheduler 表（chat tool query_events 需要）
        services["scheduler_state_store"] = SchedulerStateStore(meta_db_path)

        # M2：因子发现引擎 + proposal store + 启动期恢复 accepted 因子
        proposal_store = FactorProposalStore(meta_db_path)
        services["factor_proposal_store"] = proposal_store
        restored = restore_accepted_factors(registry, proposal_store)
        if restored:
            import logging as _logging

            _logging.getLogger(__name__).info("restored %d accepted factors from proposal_store", restored)
        services["discovery_engine"] = DiscoveryEngine(
            repository=repo,
            registry=registry,
            evaluator=evaluator,
            proposal_store=proposal_store,
            state_store=services["scheduler_state_store"],
        )

        # P4 LLM 组件（仅在 data_repo 就绪时装配；缺 llm.yaml 也用默认配置）
        llm_cfg = load_llm_config()
        services["llm_config"] = llm_cfg
        services["llm_store"] = LLMStore(meta_db_path)
        services["llm_client"] = GatewayLLMClient(
            LLMGatewayConfig(
                base_url=llm_cfg.gateway.base_url,
                anthropic_path=llm_cfg.gateway.anthropic_path,
                timeout_s=llm_cfg.gateway.timeout_s,
                max_retries=llm_cfg.gateway.max_retries,
            )
        )
        tool_registry = ToolRegistry()
        register_default_tools(tool_registry, services)
        services["llm_tool_registry"] = tool_registry
        services["llm_orchestrator"] = LLMOrchestrator(
            client=services["llm_client"],  # type: ignore[arg-type]
            tools=tool_registry,
            store=services["llm_store"],  # type: ignore[arg-type]
            max_iterations=llm_cfg.chat.max_iterations,
        )

        # M14: LLM 因子构建方向 brainstormer
        from akq_agents.services.factors.llm_brainstorm import LLMFactorBrainstormer
        services["llm_factor_brainstormer"] = LLMFactorBrainstormer(
            llm_client=services["llm_client"],
            proposal_store=services["factor_proposal_store"],
            registry=services["factor_registry"],
            evaluator=services["factor_evaluator"],
            repo=services["data_repository"],  # M19: 让 brainstorm 入库时跑 90 天 IC backfill
            model=llm_cfg.brainstorm.model,
            max_tokens=llm_cfg.brainstorm.max_tokens,
            temperature=llm_cfg.brainstorm.temperature,
        )

        # M17: Alerter（每 30 分钟巡检 NAV / data refresh / 因子衰减，触发 macOS 通知）
        from akq_agents.models.scheduler_config import SchedulerConfig as _SchedCfg
        from akq_agents.services.alerter import Alerter

        _alert_cfg = _SchedCfg().jobs.alerter
        services["alerter"] = Alerter(
            meta_db_path=meta_db_path,
            state_store=services["scheduler_state_store"],
            nav_max_abs_daily_return=_alert_cfg.nav_max_abs_daily_return,
            refresh_max_consecutive_failed=_alert_cfg.refresh_max_consecutive_failed,
            factor_decay_min_abs_ir=_alert_cfg.factor_decay_min_abs_ir,
            factor_metrics_max_stale_days=_alert_cfg.factor_metrics_max_stale_days,
        )

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


def load_scheduler_config(path: Path = SCHEDULER_CONFIG_PATH) -> SchedulerConfig:
    """加载 scheduler.yaml；文件不存在时返回默认 :class:`SchedulerConfig`。"""
    if not path.exists():
        return SchedulerConfig()
    return SchedulerConfig.from_yaml(path)


def load_llm_config(path: Path = LLM_CONFIG_PATH) -> LLMConfig:
    """加载 llm.yaml；文件不存在时返回默认 :class:`LLMConfig`。"""
    if not path.exists():
        return LLMConfig()
    return LLMConfig.from_yaml(path)


def load_web_config(path: Path = WEB_CONFIG_PATH) -> WebConfig:
    """加载 web.yaml；文件不存在时返回默认 :class:`WebConfig`。"""
    if not path.exists():
        return WebConfig()
    return WebConfig.from_yaml(path)


def build_workflow(config_path: Path = CONFIG_PATH):
    config = AppConfig.from_yaml(config_path)
    data_config = load_data_config()
    services = build_services(config, data_config=data_config)

    # 让 calendar 一次性 bootstrap：优先在线 AKShare，失败 fallback 用本地 parquet 分区
    repo = services.get("data_repository")
    if repo is not None:
        _bootstrap_calendar_safely(repo)

    return QuantWorkflow(config, services), config


def _bootstrap_calendar_safely(repo) -> None:
    """优先在线 bootstrap；失败 fallback 用本地 ohlcv 分区目录推交易日。"""
    try:
        repo._calendar.bootstrap(lambda: repo._gateway.fetch_trading_dates())
        return
    except Exception:
        pass  # 离线/限频 → fallback

    # fallback：扫描 data/parquet/ohlcv/date=YYYY-MM-DD/
    from datetime import date as _date

    ohlcv_root = getattr(repo, "_ohlcv_dir", None)
    days: list[_date] = []
    if ohlcv_root is not None and ohlcv_root.exists():
        for p in ohlcv_root.glob("date=*"):
            try:
                days.append(_date.fromisoformat(p.name.split("=", 1)[1]))
            except Exception:
                continue
    if not days:
        # 实在没有本地数据，再尝试一次（让上层看到真实错误）
        repo._calendar.bootstrap(lambda: repo._gateway.fetch_trading_dates())
        return
    repo._calendar.bootstrap(lambda: days)


def build_daemon(
    *,
    install_signals: bool = True,
) -> QuantDaemon:
    """P2 装配 :class:`QuantDaemon`，含 P3 portfolio pipeline 组件。

    要求 ``config/data.yaml`` 存在（``meta.db`` 路径由 DataConfig 决定）。
    """
    data_config = load_data_config()
    if data_config is None:
        raise RuntimeError("data config not found: config/data.yaml is required for daemon")
    workflow, _ = build_workflow()
    scheduler_cfg = load_scheduler_config()

    repo: DataRepository = workflow.services["data_repository"]  # type: ignore[assignment]
    # calendar 已在 build_workflow 阶段安全 bootstrap；此处不再重复

    base_dir = repo._base_dir
    meta_db_path = base_dir / "meta.db"
    state_store = SchedulerStateStore(meta_db_path)
    daemon_state_file = DaemonStateFile(base_dir / "daemon_state.json")
    retry_worker = RetryWorker(repository=repo, gateway=repo._gateway)

    # daemon 跑的 services 在 workflow.services 基础上扩展（workflow / retry_worker）
    daemon_services: dict[str, object] = dict(workflow.services)
    daemon_services["workflow"] = workflow
    daemon_services["retry_worker"] = retry_worker

    return QuantDaemon(
        config=scheduler_cfg,
        services=daemon_services,
        state_store=state_store,
        daemon_state_file=daemon_state_file,
        is_trading_day=repo.is_trading_day,
        version="akq-agents 0.3.0-P3a",
        install_signals=install_signals,
    )
