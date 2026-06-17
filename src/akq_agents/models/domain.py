from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class MarketSnapshot:
    symbol: str
    close: float
    volume: float
    timestamp: datetime
    extras: Dict[str, float] = field(default_factory=dict)


@dataclass
class FactorScore:
    symbol: str
    factor_name: str
    value: float
    timestamp: datetime


@dataclass
class BacktestReport:
    factor_name: str
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    score: float
    timestamp: datetime
    turnover: float = 0.0
    ic: float = 0.0
    rank_ic: float = 0.0


@dataclass
class PortfolioRecommendation:
    symbol: str
    weight: float
    score: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class DailyAdvice:
    generated_at: datetime
    summary: str
    watchlist: List[str]
    buy_candidates: List[str]
    reduce_candidates: List[str]
    risk_notes: List[str]
