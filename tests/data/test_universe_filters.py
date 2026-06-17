"""UniverseManager 与各过滤器单元测试。

P1.5 重构后：
- ``SuspendedFilter`` / ``PriceRangeFilter`` 缺值时**透明跳过**（return True），
  不再 fail-closed —— 因为新浪源没有这两个字段，全员 fail-closed 会清空 universe。
- ``listing_date`` 直接从 spot 行取，不再调用 ``fetch_individual_info``。
- spot DataFrame 列名为英文 ``symbol/name/listing_date``。
"""

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

    def fetch_spot(self) -> pd.DataFrame:
        return pd.DataFrame(self.spot_rows)

    def fetch_st_list(self) -> list[str]:
        return self.st_symbols

    def fetch_individual_info(self, symbol: str) -> dict:  # noqa: ARG002 — kept for Protocol compat
        return {"listing_date": None, "is_suspended": None}


# ----------------------------------------------------------- STFilter

def test_st_filter_keeps_non_st_symbol() -> None:
    f = STFilter(st_set={"000001"})
    assert f.keep({"symbol": "000002"}) is True


def test_st_filter_excludes_st_symbol() -> None:
    f = STFilter(st_set={"000001"})
    assert f.keep({"symbol": "000001"}) is False


def test_st_filter_missing_symbol_is_fail_closed() -> None:
    f = STFilter(st_set={"000001"})
    assert f.keep({}) is False


# ----------------------------------------------------------- ListingAgeFilter

def test_listing_age_filter_keeps_old_enough_stock() -> None:
    f = ListingAgeFilter(today=TODAY, min_days=180)
    assert f.keep({"listing_date": TODAY - timedelta(days=180)}) is True


def test_listing_age_filter_excludes_new_stock() -> None:
    f = ListingAgeFilter(today=TODAY, min_days=180)
    assert f.keep({"listing_date": TODAY - timedelta(days=179)}) is False


def test_listing_age_filter_missing_listing_date_is_fail_closed() -> None:
    f = ListingAgeFilter(today=TODAY, min_days=180)
    assert f.keep({"listing_date": None}) is False


# ----------------------------------------------------------- SuspendedFilter

def test_suspended_filter_keeps_active_stock() -> None:
    f = SuspendedFilter()
    assert f.keep({"is_suspended": False}) is True


def test_suspended_filter_excludes_suspended_stock() -> None:
    f = SuspendedFilter()
    assert f.keep({"is_suspended": True}) is False


def test_suspended_filter_missing_status_transparently_skips() -> None:
    """新行为：is_suspended=None 视为'无数据透明跳过'，返回 True。"""
    f = SuspendedFilter()
    assert f.keep({"is_suspended": None}) is True


# ----------------------------------------------------------- PriceRangeFilter

def test_price_range_filter_keeps_price_in_range() -> None:
    f = PriceRangeFilter(min_price=5.0, max_price=10.0)
    assert f.keep({"price": 7.5}) is True


def test_price_range_filter_excludes_price_out_of_range() -> None:
    f = PriceRangeFilter(min_price=5.0, max_price=10.0)
    assert f.keep({"price": 4.9}) is False
    assert f.keep({"price": 10.1}) is False


def test_price_range_filter_missing_price_transparently_skips() -> None:
    """新行为：price=None 视为'无数据透明跳过'，返回 True。"""
    f = PriceRangeFilter(min_price=5.0, max_price=10.0)
    assert f.keep({"price": None}) is True


# ----------------------------------------------------------- UniverseManager

def test_universe_manager_builds_snapshot_with_reason_codes() -> None:
    """新接口：spot 含 symbol/name/listing_date；is_suspended/price 不再过滤。"""
    gateway = FakeGateway(
        spot_rows=[
            {"symbol": "000001", "name": "A", "listing_date": TODAY - timedelta(days=400)},  # 留
            {"symbol": "000002", "name": "B", "listing_date": TODAY - timedelta(days=400)},  # ST
            {"symbol": "000003", "name": "C", "listing_date": TODAY - timedelta(days=100)},  # 太新
            {"symbol": "000006", "name": "F", "listing_date": TODAY - timedelta(days=400)},  # ST
        ],
        st_symbols=["000002", "000006"],
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
        "000006": "ST",
    }


def test_universe_manager_skips_st_filter_when_include_st_true() -> None:
    gateway = FakeGateway(
        spot_rows=[
            {"symbol": "000002", "name": "B", "listing_date": TODAY - timedelta(days=400)},
        ],
        st_symbols=["000002"],
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


def test_universe_manager_listing_date_from_spot_no_individual_info_call() -> None:
    """关键：listing_date 直接来自 spot 行，不再调 fetch_individual_info。"""
    calls = {"individual_info": 0}

    @dataclass
    class TrackingGateway(FakeGateway):
        def fetch_individual_info(self, symbol: str) -> dict:  # noqa: ARG002
            calls["individual_info"] += 1
            return {"listing_date": None, "is_suspended": None}

    gateway = TrackingGateway(
        spot_rows=[
            {"symbol": "000001", "name": "A", "listing_date": TODAY - timedelta(days=400)},
        ],
        st_symbols=[],
    )
    manager = UniverseManager(
        gateway=gateway,
        config=UniverseFilterConfig(min_listing_days=180),
    )
    snapshot = manager.build_snapshot(TODAY)

    assert snapshot.symbols == ["000001"]
    assert calls["individual_info"] == 0  # 性能关键：不再调用
