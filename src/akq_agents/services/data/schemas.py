"""数据层标准化 pydantic schema。

对应 P1 spec §4 schema 部分。这些 schema 是后续阶段（P2 调度、P3 因子注册表、
P5 Web 控制台）依赖的稳定契约，**字段一旦发布禁止破坏性变更**。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    """单股单日行情，是缓存层 Parquet 的核心行。"""

    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    turnover: float | None = None


class UniverseSnapshot(BaseModel):
    """某日可交易股票池快照。

    ``excluded`` 中每只被排除股票对应一个 reason_code，便于 Web 端展示原因。
    """

    date: date
    symbols: list[str]
    excluded: dict[str, str] = Field(default_factory=dict)


class DataHealth(BaseModel):
    """数据层整体健康状况，供 ``data status`` CLI 和后续 Web 控制台直接渲染。"""

    last_full_refresh: datetime | None = None
    universe_size_today: int = 0
    ohlcv_coverage_today: float = 0.0
    financials_freshness_days: int = -1
    pending_retries: int = 0
    unresolved_errors_24h: int = 0
    health: Literal["OK", "DEGRADED", "FAILED"] = "FAILED"


class RefreshResult(BaseModel):
    """单次 ``refresh_daily`` 调用的统计结果，供 CLI/调度器日志使用。"""

    target_date: date
    requested: int = 0
    fetched: int = 0
    cached_hit: int = 0
    failed: int = 0
    skipped_non_trading_day: bool = False
    quality_passed: bool = False
    duration_s: float = 0.0
