from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from freezegun import freeze_time

from akq_agents.models.data_config import DataConfig, QualityConfig
from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.calendar import TradingCalendar
from akq_agents.services.data.exceptions import DataNotReady, FetchError, QualityCheckFailed
from akq_agents.services.data.quality import QualityGate
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.data.schemas import UniverseSnapshot
from akq_agents.services.data.universe import UniverseManager


def _make_snapshot(d: date, symbols: list[str]) -> UniverseSnapshot:
    return UniverseSnapshot(date=d, symbols=symbols, excluded={})


def _make_ohlcv(symbol: str, d: date) -> pd.DataFrame:
    base = int(symbol[-2:]) if symbol[-2:].isdigit() else 1
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(d)],
            "open": [10.0 + base],
            "high": [11.0 + base],
            "low": [9.0 + base],
            "close": [10.5 + base],
            "volume": [1000.0 + base],
            "amount": [10000.0 + base],
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]:
    cfg = DataConfig(
        base_dir=str(tmp_path),
        quality=QualityConfig(min_universe_size=1, max_null_rate=0.5),
    )
    gateway = MagicMock(spec=AKShareGateway)
    calendar = MagicMock(spec=TradingCalendar)
    calendar.is_trading_day.return_value = True
    calendar.trading_days_between.side_effect = lambda start, end: [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]
    universe_mgr = MagicMock(spec=UniverseManager)
    universe_mgr.build_snapshot.return_value = _make_snapshot(date(2026, 6, 17), ["600519", "000001"])
    quality_gate = MagicMock(spec=QualityGate)
    quality_gate.check.return_value = {"row_count": True, "null_rate": True, "close_range": True}
    repository = DataRepository(cfg, gateway, calendar, universe_mgr, quality_gate, tmp_path)
    return repository, gateway, calendar, universe_mgr, quality_gate


def _refresh_state_rows(meta_db: Path) -> list[tuple[str, str, str, int]]:
    with sqlite3.connect(meta_db) as conn:
        return conn.execute(
            "SELECT target_date, ts, status, rows FROM refresh_state ORDER BY target_date"
        ).fetchall()


def _fetch_errors_rows(meta_db: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(meta_db) as conn:
        return conn.execute(
            "SELECT symbol, endpoint, reason_code, message FROM fetch_errors"
        ).fetchall()


def test_refresh_daily_happy_path(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, _, universe_mgr, _ = repo
    target_date = date(2026, 6, 17)
    symbols = [f"00000{i}" for i in range(5)]
    universe_mgr.build_snapshot.return_value = _make_snapshot(target_date, symbols)
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, target_date)

    result = repository.refresh_daily(target_date)

    assert result.target_date == target_date
    assert result.requested == 5
    assert result.fetched == 5
    assert result.failed == 0
    assert result.quality_passed is True
    parquet_path = tmp_path_from_repo(repository) / "parquet" / "ohlcv" / f"date={target_date.isoformat()}" / "part.parquet"
    assert parquet_path.exists()
    universe_path = tmp_path_from_repo(repository) / "parquet" / "universe" / f"date={target_date.isoformat()}" / "snap.parquet"
    assert universe_path.exists()
    refresh_rows = _refresh_state_rows(tmp_path_from_repo(repository) / "meta.db")
    assert len(refresh_rows) == 1
    assert refresh_rows[0][0] == target_date.isoformat()
    assert refresh_rows[0][2] == "ok"
    assert refresh_rows[0][3] == 5


def test_refresh_daily_non_trading_day_skipped(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, calendar, universe_mgr, quality_gate = repo
    calendar.is_trading_day.return_value = False

    result = repository.refresh_daily(date(2026, 6, 21))

    assert result.skipped_non_trading_day is True
    assert result.requested == 0
    assert result.fetched == 0
    gateway.fetch_ohlcv.assert_not_called()
    universe_mgr.build_snapshot.assert_not_called()
    quality_gate.check.assert_not_called()
    assert not (tmp_path_from_repo(repository) / "meta.db").exists()


def test_refresh_daily_partial_failure(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, _, universe_mgr, _ = repo
    target_date = date(2026, 6, 17)
    symbols = ["000001", "000002", "000003"]
    universe_mgr.build_snapshot.return_value = _make_snapshot(target_date, symbols)

    def fetch(symbol: str, start: date, end: date) -> pd.DataFrame:
        if symbol == "000002":
            raise FetchError(reason_code="NETWORK", message="boom", symbol=symbol)
        return _make_ohlcv(symbol, target_date)

    gateway.fetch_ohlcv.side_effect = fetch

    result = repository.refresh_daily(target_date)

    assert result.fetched == 2
    assert result.failed == 1
    rows = _fetch_errors_rows(tmp_path_from_repo(repository) / "meta.db")
    assert rows == [("000002", "ohlcv", "NETWORK", "boom")]
    frame = repository.get_ohlcv(["000001", "000003"], target_date, target_date)
    assert frame["symbol"].tolist() == ["000001", "000003"]


def test_refresh_daily_quality_fail_no_parquet_write(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, _, universe_mgr, quality_gate = repo
    target_date = date(2026, 6, 17)
    universe_mgr.build_snapshot.return_value = _make_snapshot(target_date, ["000001", "000002"])
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, target_date)
    quality_gate.check.side_effect = QualityCheckFailed({"row_count": False, "null_rate": True})

    result = repository.refresh_daily(target_date)

    assert result.quality_passed is False
    assert not (
        tmp_path_from_repo(repository) / "parquet" / "ohlcv" / f"date={target_date.isoformat()}" / "part.parquet"
    ).exists()
    with sqlite3.connect(tmp_path_from_repo(repository) / "meta.db") as conn:
        row = conn.execute(
            "SELECT target_date, passed FROM data_quality_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == (target_date.isoformat(), 0)


def test_refresh_daily_idempotent(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, _, universe_mgr, _ = repo
    target_date = date(2026, 6, 17)
    universe_mgr.build_snapshot.return_value = _make_snapshot(target_date, ["000001", "000002"])
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, target_date)

    first = repository.refresh_daily(target_date)
    second = repository.refresh_daily(target_date)

    assert first.fetched == 2
    assert second.cached_hit == 2
    assert second.fetched == 0
    assert gateway.fetch_ohlcv.call_count == 2


def test_get_ohlcv_returns_cached_frame(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, calendar, universe_mgr, _ = repo
    days = [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]
    symbols = ["000001", "000002"]
    calendar.trading_days_between.return_value = days
    universe_mgr.build_snapshot.side_effect = lambda d: _make_snapshot(d, symbols)
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, start)

    for day in days:
        repository.refresh_daily(day)

    frame = repository.get_ohlcv(symbols, days[0], days[-1])

    assert list(frame.columns) == ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
    assert len(frame) == 6
    assert frame[["symbol", "date"]].values.tolist() == [
        ["000001", pd.Timestamp(days[0]).date()],
        ["000001", pd.Timestamp(days[1]).date()],
        ["000001", pd.Timestamp(days[2]).date()],
        ["000002", pd.Timestamp(days[0]).date()],
        ["000002", pd.Timestamp(days[1]).date()],
        ["000002", pd.Timestamp(days[2]).date()],
    ]


def test_get_ohlcv_missing_raises_data_not_ready(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, calendar, universe_mgr, _ = repo
    target_date = date(2026, 6, 17)
    next_date = date(2026, 6, 18)
    symbols = ["000001", "000002"]
    calendar.trading_days_between.return_value = [target_date, next_date]
    universe_mgr.build_snapshot.return_value = _make_snapshot(target_date, symbols)
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, target_date)
    repository.refresh_daily(target_date)

    with pytest.raises(DataNotReady) as exc_info:
        repository.get_ohlcv(symbols, target_date, next_date)

    assert exc_info.value.missing == {
        "000001": [next_date],
        "000002": [next_date],
    }


def test_get_universe_missing_raises_data_not_ready(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, _, _, _, _ = repo

    with pytest.raises(DataNotReady) as exc_info:
        repository.get_universe(date(2026, 6, 17))

    assert exc_info.value.missing == {"_universe": [date(2026, 6, 17)]}


def test_quality_report_ok_after_recent_refresh(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, _, universe_mgr, _ = repo
    today = date(2026, 6, 17)
    symbols = ["000001", "000002", "000003", "000004"]
    universe_mgr.build_snapshot.return_value = _make_snapshot(today, symbols)
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, today)

    with freeze_time("2026-06-17 18:00:00"):
        repository.refresh_daily(today)
        report = repository.quality_report()

    assert report.health == "OK"
    assert report.universe_size_today == 4
    assert report.ohlcv_coverage_today == pytest.approx(1.0)
    assert report.last_full_refresh == datetime(2026, 6, 17, 18, 0, 0)


def test_quality_report_failed_with_no_data(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, _, _, _, _ = repo

    with freeze_time("2026-06-17 18:00:00"):
        report = repository.quality_report()

    assert report.health == "FAILED"
    assert report.universe_size_today == 0
    assert report.ohlcv_coverage_today == 0.0
    assert report.last_full_refresh is None


def test_pending_retries_counts_unresolved(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, _, _, _, _ = repo
    meta_db = tmp_path_from_repo(repository) / "meta.db"
    repository._ensure_storage()  # noqa: SLF001
    with sqlite3.connect(meta_db) as conn:
        conn.execute(
            "INSERT INTO fetch_errors (ts, symbol, endpoint, reason_code, message, retry_count, resolved) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-17T10:00:00", "000001", "ohlcv", "NETWORK", "x", 0, 0),
        )
        conn.execute(
            "INSERT INTO fetch_errors (ts, symbol, endpoint, reason_code, message, retry_count, resolved) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-17T10:00:00", "000002", "ohlcv", "NETWORK", "y", 0, 1),
        )
        conn.commit()

    assert repository.pending_retries() == 1


def test_bootstrap_history_iterates_and_resumes(
    repo: tuple[DataRepository, MagicMock, MagicMock, MagicMock, MagicMock]
) -> None:
    repository, gateway, calendar, universe_mgr, _ = repo
    days = [date(2026, 6, 13) + timedelta(days=offset) for offset in range(5)]
    calendar.trading_days_between.return_value = days
    universe_mgr.build_snapshot.side_effect = lambda d: _make_snapshot(d, ["000001", "000002"])
    gateway.fetch_ohlcv.side_effect = lambda symbol, start, end: _make_ohlcv(symbol, start)
    progress: list[tuple[int, int]] = []

    repository.bootstrap_history(5, progress_cb=lambda done, total: progress.append((done, total)))
    first_call_count = gateway.fetch_ohlcv.call_count

    with sqlite3.connect(tmp_path_from_repo(repository) / "meta.db") as conn:
        conn.execute("DELETE FROM refresh_state WHERE target_date = ?", (days[2].isoformat(),))
        conn.commit()

    repository.bootstrap_history(5)

    assert first_call_count == 10
    assert gateway.fetch_ohlcv.call_count == 12
    assert progress == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def tmp_path_from_repo(repository: DataRepository) -> Path:
    return repository._base_dir  # noqa: SLF001
