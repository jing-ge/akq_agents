from __future__ import annotations

import sys
from datetime import date

import pytest

from akq_agents.services.data.calendar import TradingCalendar, default_load_fn_via_akshare
from akq_agents.services.data.exceptions import FetchError


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar()


@pytest.fixture
def sample_days() -> list[date]:
    return [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 8),
    ]


def bootstrap_days(calendar: TradingCalendar, sample_days: list[date]) -> TradingCalendar:
    calendar.bootstrap(lambda: sample_days)
    return calendar


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("is_trading_day", (date(2026, 1, 2),)),
        ("previous_trading_day", (date(2026, 1, 2),)),
        ("next_trading_day", (date(2026, 1, 2),)),
        ("trading_days_between", (date(2026, 1, 2), date(2026, 1, 8))),
    ],
)
def test_read_methods_raise_when_not_bootstrapped(
    calendar: TradingCalendar, method_name: str, args: tuple[date, ...]
) -> None:
    with pytest.raises(RuntimeError, match="TradingCalendar not bootstrapped"):
        getattr(calendar, method_name)(*args)


def test_is_trading_day_returns_true_and_false(calendar: TradingCalendar, sample_days: list[date]) -> None:
    bootstrap_days(calendar, sample_days)

    assert calendar.is_trading_day(date(2026, 1, 5)) is True
    assert calendar.is_trading_day(date(2026, 1, 7)) is False


def test_previous_trading_day_handles_trading_and_non_trading_dates(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    assert calendar.previous_trading_day(date(2026, 1, 6)) == date(2026, 1, 5)
    assert calendar.previous_trading_day(date(2026, 1, 7)) == date(2026, 1, 6)


def test_previous_trading_day_raises_when_before_range(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    with pytest.raises(ValueError):
        calendar.previous_trading_day(date(2026, 1, 2))


def test_next_trading_day_handles_trading_and_non_trading_dates(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    assert calendar.next_trading_day(date(2026, 1, 5)) == date(2026, 1, 6)
    assert calendar.next_trading_day(date(2026, 1, 7)) == date(2026, 1, 8)


def test_next_trading_day_raises_when_after_range(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    with pytest.raises(ValueError):
        calendar.next_trading_day(date(2026, 1, 8))


def test_trading_days_between_returns_closed_interval(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    assert calendar.trading_days_between(date(2026, 1, 2), date(2026, 1, 6)) == [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_trading_days_between_returns_empty_for_reverse_interval(
    calendar: TradingCalendar, sample_days: list[date]
) -> None:
    bootstrap_days(calendar, sample_days)

    assert calendar.trading_days_between(date(2026, 1, 8), date(2026, 1, 2)) == []


def test_bootstrap_is_idempotent(calendar: TradingCalendar, sample_days: list[date]) -> None:
    calendar.bootstrap(lambda: sample_days)
    first = calendar.trading_days_between(date(2026, 1, 1), date(2026, 1, 31))

    calendar.bootstrap(lambda: sample_days)
    second = calendar.trading_days_between(date(2026, 1, 1), date(2026, 1, 31))

    assert first == second == sample_days


def test_default_load_fn_via_akshare_raises_fetch_error_when_akshare_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "akshare", None)

    load_fn = default_load_fn_via_akshare()

    with pytest.raises(FetchError) as exc_info:
        load_fn()

    assert exc_info.value.reason_code == "UNKNOWN"
    assert exc_info.value.message == "akshare not installed"
