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
        is_st = info.get("is_st")
        if symbol is None or is_st is None:
            return False
        return symbol not in self.st_set and is_st is False


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
    name: str = "suspended_filter"
    reason_code: str = "SUSPENDED"

    def keep(self, info: dict) -> bool:
        is_suspended = info.get("is_suspended")
        if is_suspended is None:
            return False
        return is_suspended is False


@dataclass
class PriceRangeFilter:
    min_price: float
    max_price: float
    name: str = "price_range_filter"
    reason_code: str = "PRICE_OUT_OF_RANGE"

    def keep(self, info: dict) -> bool:
        price = info.get("price")
        if price is None:
            return False
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
            symbol = str(row["代码"])
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
        individual_info = self.gateway.fetch_individual_info(symbol)
        return {
            "symbol": symbol,
            "price": row.get("最新价"),
            "listing_date": individual_info.get("listing_date"),
            "is_suspended": individual_info.get("is_suspended"),
            "is_st": symbol in st_set,
        }
