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


class PortfolioConfig(BaseModel):
    """PortfolioOptimizer 参数. 对应 config/system.yaml `portfolio:` 段.

    调这些值可直接影响每日组合的换手 / 集中度, 不用改代码.
    """
    top_n: int = 50
    max_single_weight: float = 0.05
    max_industry_weight: float = 0.30
    # M25: 0.7→0.4 让权重更黏 prev, 单日换手从 ~20% 降到 ~10% 内.
    # 1.0 = 完全采纳新权重 (原始 inverse-vol); 0.0 = 完全不动 prev.
    turnover_aversion: float = 0.4


class TradeListSectionConfig(BaseModel):
    """TradeList 生成参数. 对应 config/system.yaml `trade_list:` 段.

    散户可根据实际本金 / 执行力调 min_trade_amount 和 min_weight_change,
    控制每日可执行 BUY/SELL 条数.
    """
    assumed_capital: float = 100_000.0
    lot_size: int = 100
    # M25: 200→2000, 假定 10 万本金 + 单票 1-5% 权重, 相当于
    # "调仓金额 ≥ 单票平均权重 × 4" 才动手, 砍掉每天 50%+ 碎单.
    min_trade_amount: float = 2000.0
    # M25: 权重变化 < 0.5% (500 元 / 10 万本金) 且非建仓/清仓 → 强制 HOLD,
    # 避免每日堆一堆微调噪音单.
    min_weight_change: float = 0.005


class BacktestConfig(BaseModel):
    engine: str = "mock"
    commission: float = 0.0003
    slippage: float = 0.0005
    initial_capital: float = 1000000
    start_date: str | None = None
    end_date: str | None = None


class ServicesConfig(BaseModel):
    use_mock_data: bool = True
    use_mock_backtest: bool = True
    strict_real_services: bool = False


class AppConfig(BaseModel):
    system: SystemInfo
    universe: UniverseConfig
    research: ResearchConfig
    risk: RiskConfig
    portfolio: PortfolioConfig = PortfolioConfig()
    trade_list: TradeListSectionConfig = TradeListSectionConfig()
    backtest: BacktestConfig
    services: ServicesConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        with open(path, encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return cls.model_validate(payload)
