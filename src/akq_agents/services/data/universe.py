"""股票池过滤与构建。

P1.5 重构后，``UniverseManager`` 的输入契约改为：
``gateway.fetch_spot()`` 返回 ``[symbol, name, listing_date]``（无 price）。

- ``listing_date`` 现在来自 spot 行，**不再调** ``fetch_individual_info``，
  避免 5000 次个股接口请求且兼容东方财富网络受限场景。
- ``price`` 字段不再可用 → :class:`PriceRangeFilter` 在缺 price 时**透明跳过**
  而非 fail-closed。价格越界过滤改由后续读 OHLCV 时做二次清洗（YAGNI：本期不
  做，后期补）。
- ``is_suspended`` 没有稳定数据源 → :class:`SuspendedFilter` 同样透明跳过。
- ``is_st`` 列表当前为空（gateway stub）→ :class:`STFilter` 自然不排除任何
  symbol，但保留代码避免后续接 ST 源时再次改造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import pandas as pd

from akq_agents.models.data_config import UniverseFilterConfig
from akq_agents.services.data.schemas import UniverseSnapshot


class GatewayLike(Protocol):
    def fetch_spot(self) -> pd.DataFrame: ...

    def fetch_st_list(self) -> list[str]: ...

    def fetch_individual_info(self, symbol: str) -> dict: ...


class UniverseFilter(Protocol):
    name: str
    reason_code: str

    def keep(self, info: dict) -> bool: ...


@dataclass
class STFilter:
    name: str = "st_filter"
    reason_code: str = "ST"
    st_set: set[str] = field(default_factory=set)

    def keep(self, info: dict) -> bool:
        symbol = info.get("symbol")
        if symbol is None:
            return False
        # is_st 直接由 st_set 推导：缺数据时 st_set 为空 → 不排除任何 symbol
        return symbol not in self.st_set


@dataclass
class ListingAgeFilter:
    today: date
    min_days: int
    name: str = "listing_age_filter"
    reason_code: str = "LISTING_TOO_NEW"

    def keep(self, info: dict) -> bool:
        listing_date = info.get("listing_date")
        if listing_date is None:
            return False
        return (self.today - listing_date).days >= self.min_days


@dataclass
class SuspendedFilter:
    """is_suspended 缺失时透明跳过（None 视为不过滤），否则要求 False。"""

    name: str = "suspended_filter"
    reason_code: str = "SUSPENDED"

    def keep(self, info: dict) -> bool:
        is_suspended = info.get("is_suspended")
        if is_suspended is None:
            return True  # 缺数据透明跳过，避免把全市场都过滤掉
        return is_suspended is False


@dataclass
class PriceRangeFilter:
    """price 缺失时透明跳过（None 视为不过滤）。"""

    min_price: float
    max_price: float
    name: str = "price_range_filter"
    reason_code: str = "PRICE_OUT_OF_RANGE"

    def keep(self, info: dict) -> bool:
        price = info.get("price")
        if price is None:
            return True  # 缺数据透明跳过
        return self.min_price <= price <= self.max_price


class UniverseManager:
    def __init__(self, gateway: GatewayLike, config: UniverseFilterConfig) -> None:
        self.gateway = gateway
        self.config = config

    def build_snapshot(self, d: date) -> UniverseSnapshot:
        spot_df = self.gateway.fetch_spot()
        st_set = set() if self.config.include_st else set(self.gateway.fetch_st_list())
        filters = self._build_filters(d, st_set)

        symbols: list[str] = []
        excluded: dict[str, str] = {}

        for _, row in spot_df.iterrows():
            symbol = str(row["symbol"])
            info = self._build_info(symbol=symbol, row=row, st_set=st_set)

            for universe_filter in filters:
                if universe_filter.keep(info):
                    continue
                excluded[symbol] = universe_filter.reason_code
                break
            else:
                symbols.append(symbol)

        return UniverseSnapshot(date=d, symbols=symbols, excluded=excluded)

    def _build_filters(self, d: date, st_set: set[str]) -> list[UniverseFilter]:
        filters: list[UniverseFilter] = []
        if not self.config.include_st:
            filters.append(STFilter(st_set=st_set))
        if not self.config.include_new:
            filters.append(ListingAgeFilter(today=d, min_days=self.config.min_listing_days))
        filters.append(SuspendedFilter())
        filters.append(
            PriceRangeFilter(
                min_price=self.config.min_price,
                max_price=self.config.max_price,
            )
        )
        return filters

    def _build_info(self, symbol: str, row: pd.Series, st_set: set[str]) -> dict:
        # listing_date 现在直接来自 spot 行（沪深交易所列表），不再调 individual_info
        listing_date = row.get("listing_date") if "listing_date" in row.index else None
        return {
            "symbol": symbol,
            "name": row.get("name") if "name" in row.index else None,
            "price": row.get("price") if "price" in row.index else None,
            "listing_date": listing_date,
            "is_suspended": None,  # 当前无稳定源
            "is_st": symbol in st_set,
        }
