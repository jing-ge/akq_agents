from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from akq_agents.models.data_config import UniverseFilterConfig
from akq_agents.services.data.universe import (
    ListingAgeFilter,
    PriceRangeFilter,
    STFilter,
    SuspendedFilter,
    UniverseManager,
)

TODAY = date(2026, 6, 17)


@dataclass
class FakeGateway:
    spot_rows: list[dict]
    st_symbols: list[str]
    info_map: dict[str, dict]

    def fetch_spot(self) -> pd.DataFrame:
        return pd.DataFrame(self.spot_rows)

    def fetch_st_list(self) -> list[str]:
        return self.st_symbols

    def fetch_individual_info(self, symbol: str) -> dict:
        return self.info_map[symbol]


def test_st_filter_keeps_non_st_symbol() -> None:
    st_filter = STFilter(st_set={"000001"})

    assert st_filter.keep({"symbol": "000002", "is_st": False}) is True


def test_st_filter_excludes_st_symbol() -> None:
    st_filter = STFilter(st_set={"000001"})

    assert st_filter.keep({"symbol": "000001", "is_st": True}) is False


def test_st_filter_missing_symbol_is_fail_closed() -> None:
    st_filter = STFilter(st_set={"000001"})

    assert st_filter.keep({"is_st": False}) is False


def test_listing_age_filter_keeps_old_enough_stock() -> None:
    age_filter = ListingAgeFilter(today=TODAY, min_days=180)

    assert age_filter.keep({"listing_date": TODAY - timedelta(days=180)}) is True


def test_listing_age_filter_excludes_new_stock() -> None:
    age_filter = ListingAgeFilter(today=TODAY, min_days=180)

    assert age_filter.keep({"listing_date": TODAY - timedelta(days=179)}) is False


def test_listing_age_filter_missing_listing_date_is_fail_closed() -> None:
    age_filter = ListingAgeFilter(today=TODAY, min_days=180)

    assert age_filter.keep({"listing_date": None}) is False


def test_suspended_filter_keeps_active_stock() -> None:
    suspended_filter = SuspendedFilter()

    assert suspended_filter.keep({"is_suspended": False}) is True


def test_suspended_filter_excludes_suspended_stock() -> None:
    suspended_filter = SuspendedFilter()

    assert suspended_filter.keep({"is_suspended": True}) is False


def test_suspended_filter_missing_status_is_fail_closed() -> None:
    suspended_filter = SuspendedFilter()

    assert suspended_filter.keep({"is_suspended": None}) is False


def test_price_range_filter_keeps_price_in_range() -> None:
    price_filter = PriceRangeFilter(min_price=5.0, max_price=10.0)

    assert price_filter.keep({"price": 7.5}) is True


def test_price_range_filter_excludes_price_out_of_range() -> None:
    price_filter = PriceRangeFilter(min_price=5.0, max_price=10.0)

    assert price_filter.keep({"price": 4.9}) is False
    assert price_filter.keep({"price": 10.1}) is False


def test_price_range_filter_missing_price_is_fail_closed() -> None:
    price_filter = PriceRangeFilter(min_price=5.0, max_price=10.0)

    assert price_filter.keep({"price": None}) is False


def test_universe_manager_builds_snapshot_with_reason_codes() -> None:
    gateway = FakeGateway(
        spot_rows=[
            {"代码": "000001", "最新价": 10.0},
            {"代码": "000002", "最新价": 8.0},
            {"代码": "000003", "最新价": 6.0},
            {"代码": "000004", "最新价": 7.0},
            {"代码": "000005", "最新价": 0.8},
            {"代码": "000006", "最新价": 15.0},
        ],
        st_symbols=["000002", "000006"],
        info_map={
            "000001": {"listing_date": TODAY - timedelta(days=400), "is_suspended": False},
            "000002": {"listing_date": TODAY - timedelta(days=400), "is_suspended": False},
            "000003": {"listing_date": TODAY - timedelta(days=100), "is_suspended": False},
            "000004": {"listing_date": TODAY - timedelta(days=400), "is_suspended": True},
            "000005": {"listing_date": TODAY - timedelta(days=400), "is_suspended": False},
            "000006": {"listing_date": TODAY - timedelta(days=400), "is_suspended": True},
        },
    )
    manager = UniverseManager(
        gateway=gateway,
        config=UniverseFilterConfig(
            include_st=False,
            include_new=False,
            min_listing_days=180,
            min_price=1.0,
            max_price=12.0,
        ),
    )

    snapshot = manager.build_snapshot(TODAY)

    assert snapshot.date == TODAY
    assert snapshot.symbols == ["000001"]
    assert snapshot.excluded == {
        "000002": "ST",
        "000003": "LISTING_TOO_NEW",
        "000004": "SUSPENDED",
        "000005": "PRICE_OUT_OF_RANGE",
        "000006": "ST",
    }


def test_universe_manager_skips_st_filter_when_include_st_true() -> None:
    gateway = FakeGateway(
        spot_rows=[{"代码": "000002", "最新价": 8.0}],
        st_symbols=["000002"],
        info_map={
            "000002": {"listing_date": TODAY - timedelta(days=400), "is_suspended": False},
        },
    )
    manager = UniverseManager(
        gateway=gateway,
        config=UniverseFilterConfig(
            include_st=True,
            include_new=False,
            min_listing_days=180,
            min_price=1.0,
            max_price=12.0,
        ),
    )

    snapshot = manager.build_snapshot(TODAY)

    assert snapshot.symbols == ["000002"]
    assert snapshot.excluded == {}
