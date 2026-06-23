from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class SchedulerConfig(BaseModel):
    factor_refresh_minutes: int = 30
    backtest_refresh_minutes: int = 120
    portfolio_refresh_minutes: int = 180
    daily_advice_hour: int = 8
    daily_advice_minute: int = 30


class SystemInfo(BaseModel):
    name: str = "akq-agents"
    timezone: str = "Asia/Shanghai"
    scheduler: SchedulerConfig = SchedulerConfig()


class UniverseConfig(BaseModel):
    market: str = "cn"
    symbols: list[str]
    lookback_days: int = 120


class ResearchConfig(BaseModel):
    benchmark: str = "000300"
    rebalance_frequency: str = "daily"
    top_n_factors: int = 5
    top_n_symbols: int = 3
    min_sharpe: float = 0.8
    max_drawdown: float = 0.25
    min_ic: float = -1.0


class RiskConfig(BaseModel):
    max_single_weight: float = 0.4
    min_single_weight: float = 0.05
    max_portfolio_size: int = 5
    min_liquidity_score: float = 1.0


class BacktestConfig(BaseModel):
    engine: str = "mock"
    commission: float = 0.0003
    slippage: float = 0.0005
    initial_capital: float = 1000000
    start_date: str | None = None
    end_date: str | None = None


class StorageConfig(BaseModel):
    state_file: str = "./runtime_state.yaml"


class ServicesConfig(BaseModel):
    use_mock_data: bool = True
    use_mock_backtest: bool = True
    enable_llm: bool = False
    strict_real_services: bool = False


class AppConfig(BaseModel):
    system: SystemInfo
    universe: UniverseConfig
    research: ResearchConfig
    risk: RiskConfig
    backtest: BacktestConfig
    storage: StorageConfig
    services: ServicesConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        with open(path, encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return cls.model_validate(payload)
