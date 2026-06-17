from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    def save(self, state: dict[str, Any]) -> None:
        with open(self.path, "w", encoding="utf-8") as file:
            yaml.safe_dump(state, file, allow_unicode=True, sort_keys=False)


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    extras_yaml TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS factor_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    factor_name TEXT NOT NULL,
                    value REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    factor_name TEXT NOT NULL,
                    annual_return REAL NOT NULL,
                    sharpe REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    win_rate REAL NOT NULL,
                    score REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    weight REAL NOT NULL,
                    score REAL NOT NULL,
                    reasons_yaml TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_advices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    rendered TEXT NOT NULL,
                    payload_yaml TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def insert_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        columns = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        values = [tuple(row[column] for column in columns) for row in rows]
        with self._connect() as connection:
            connection.executemany(sql, values)
            connection.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._connect() as connection:
            cursor = connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def latest_backtest_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT ts, factor_name, annual_return, sharpe, max_drawdown, win_rate, score
            FROM backtest_reports
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

    def latest_portfolio(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT ts, symbol, weight, score, reasons_yaml
            FROM portfolio_recommendations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

    def latest_advice(self) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT ts, rendered, payload_yaml
            FROM daily_advices
            ORDER BY id DESC
            LIMIT 1
            """
        )
