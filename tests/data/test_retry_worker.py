from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.exceptions import FetchError
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.data.retry_worker import RetryPolicy, RetryWorker


@pytest.fixture
def meta_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "meta.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE fetch_errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              symbol TEXT,
              endpoint TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              message TEXT,
              retry_count INTEGER DEFAULT 0,
              resolved INTEGER DEFAULT 0,
              target_date TEXT
            )
            """
        )
        conn.commit()
    return db_path


@pytest.fixture
def repository(meta_db: Path) -> MagicMock:
    repo = MagicMock(spec=DataRepository)
    repo._meta_db_path = meta_db
    return repo


@pytest.fixture
def gateway() -> MagicMock:
    return MagicMock(spec=AKShareGateway)


@pytest.fixture
def worker(repository: MagicMock, gateway: MagicMock) -> RetryWorker:
    return RetryWorker(repository=repository, gateway=gateway)


def _insert_error(
    meta_db: Path,
    *,
    symbol: str = "000001",
    endpoint: str = "ohlcv",
    reason_code: str = "NETWORK",
    message: str = "boom",
    retry_count: int = 0,
    resolved: int = 0,
    target_date: str | None = "2026-06-17",
) -> int:
    with sqlite3.connect(meta_db) as conn:
        cursor = conn.execute(
            """
            INSERT INTO fetch_errors (
                ts, symbol, endpoint, reason_code, message, retry_count, resolved, target_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-17T10:00:00",
                symbol,
                endpoint,
                reason_code,
                message,
                retry_count,
                resolved,
                target_date,
            ),
        )
        conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _get_row(meta_db: Path, row_id: int) -> sqlite3.Row:
    with sqlite3.connect(meta_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM fetch_errors WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


def test_resolves_when_gateway_succeeds(meta_db: Path, worker: RetryWorker, gateway: MagicMock) -> None:
    row_id = _insert_error(meta_db, endpoint="spot", target_date=None)
    gateway.fetch_spot.return_value = object()

    stats = worker.run_once()

    row = _get_row(meta_db, row_id)
    assert row["resolved"] == 1
    assert row["retry_count"] == 0
    assert stats == {"scanned": 1, "resolved": 1, "still_failing": 0, "given_up": 0}


def test_increments_retry_count_on_failure(meta_db: Path, worker: RetryWorker, gateway: MagicMock) -> None:
    row_id = _insert_error(meta_db)
    gateway.fetch_ohlcv.side_effect = FetchError(reason_code="NETWORK", message="still bad")

    worker.run_once()

    row = _get_row(meta_db, row_id)
    assert row["resolved"] == 0
    assert row["retry_count"] == 1
    assert row["message"] == "still bad"


def test_gives_up_when_max_retries_reached(meta_db: Path, repository: MagicMock, gateway: MagicMock) -> None:
    row_id = _insert_error(meta_db, retry_count=2)
    gateway.fetch_ohlcv.side_effect = FetchError(reason_code="NETWORK", message="third failure")
    worker = RetryWorker(repository=repository, gateway=gateway, policy=RetryPolicy(max_retries=3))

    worker.run_once()

    row = _get_row(meta_db, row_id)
    assert row["resolved"] == 2
    assert row["retry_count"] == 3
    assert row["message"] == "third failure"


def test_skip_reason_unknown(meta_db: Path, worker: RetryWorker, gateway: MagicMock) -> None:
    row_id = _insert_error(meta_db, reason_code="UNKNOWN")

    stats = worker.run_once()

    row = _get_row(meta_db, row_id)
    assert row["resolved"] == 0
    assert row["retry_count"] == 0
    assert stats == {"scanned": 0, "resolved": 0, "still_failing": 0, "given_up": 0}
    gateway.fetch_ohlcv.assert_not_called()


def test_run_once_stats(meta_db: Path, repository: MagicMock, gateway: MagicMock) -> None:
    success_id = _insert_error(meta_db, symbol="000001")
    failing_id = _insert_error(meta_db, symbol="000002")
    give_up_id = _insert_error(meta_db, symbol="000003", retry_count=2)

    def fetch(symbol: str, start: date, end: date) -> object:
        if symbol == "000001":
            return object()
        if symbol == "000002":
            raise FetchError(reason_code="NETWORK", message="keep failing")
        raise FetchError(reason_code="NETWORK", message="give up now")

    gateway.fetch_ohlcv.side_effect = fetch
    worker = RetryWorker(repository=repository, gateway=gateway, policy=RetryPolicy(max_retries=3))

    stats = worker.run_once()

    assert stats == {"scanned": 3, "resolved": 1, "still_failing": 1, "given_up": 1}
    assert _get_row(meta_db, success_id)["resolved"] == 1
    assert _get_row(meta_db, failing_id)["resolved"] == 0
    assert _get_row(meta_db, failing_id)["retry_count"] == 1
    assert _get_row(meta_db, give_up_id)["resolved"] == 2
    assert _get_row(meta_db, give_up_id)["retry_count"] == 3


def test_unknown_endpoint_does_not_resolve(meta_db: Path, worker: RetryWorker) -> None:
    row_id = _insert_error(meta_db, endpoint="weird", target_date=None)

    stats = worker.run_once()

    row = _get_row(meta_db, row_id)
    assert row["resolved"] == 0
    assert row["retry_count"] == 1
    assert stats == {"scanned": 1, "resolved": 0, "still_failing": 1, "given_up": 0}
