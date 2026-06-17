"""DataRepository：P1 数据层缓存读写入口。"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from akq_agents.models.data_config import DataConfig
from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.calendar import TradingCalendar
from akq_agents.services.data.exceptions import DataNotReady, FetchError, QualityCheckFailed
from akq_agents.services.data.quality import QualityGate
from akq_agents.services.data.schemas import DataHealth, RefreshResult, UniverseSnapshot
from akq_agents.services.data.universe import UniverseManager

_FETCH_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  symbol TEXT,
  target_date TEXT,
  status TEXT NOT NULL,
  message TEXT
);
"""

_FETCH_ERRORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT,
  endpoint TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  message TEXT,
  retry_count INTEGER DEFAULT 0,
  resolved INTEGER DEFAULT 0
);
"""

_DATA_QUALITY_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_quality_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  target_date TEXT NOT NULL,
  passed INTEGER NOT NULL,
  checks_json TEXT NOT NULL
);
"""

_REFRESH_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_state (
  target_date TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  status TEXT NOT NULL,
  rows INTEGER NOT NULL
);
"""

_OHLCV_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
_OHLCV_FILE_COLUMNS = ["symbol", "open", "high", "low", "close", "volume", "amount"]


class DataRepository:
    """统一管理数据缓存的读写、元数据和健康状态。"""

    def __init__(
        self,
        config: DataConfig,
        gateway: AKShareGateway,
        calendar: TradingCalendar,
        universe_manager: UniverseManager,
        quality_gate: QualityGate,
        base_dir: Path,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._calendar = calendar
        self._universe_manager = universe_manager
        self._quality_gate = quality_gate
        self._base_dir = Path(base_dir)
        self._parquet_dir = self._base_dir / "parquet"
        self._ohlcv_dir = self._parquet_dir / "ohlcv"
        self._universe_dir = self._parquet_dir / "universe"
        self._calendar_path = self._parquet_dir / "trading_calendar.parquet"
        self._meta_db_path = self._base_dir / "meta.db"

    def get_ohlcv(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        """只读缓存中的 OHLCV；缺任一交易日时抛 ``DataNotReady``。"""
        self._ensure_storage()
        trading_days = self._calendar.trading_days_between(start, end)
        missing = self._missing_ohlcv(symbols, trading_days)
        if missing:
            raise DataNotReady(missing)
        if not trading_days or not symbols:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        dataset = ds.dataset(self._ohlcv_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(symbols),
            columns=_OHLCV_FILE_COLUMNS + ["date"],
        )
        frame = table.to_pandas()
        if frame.empty:
            return pd.DataFrame({column: pd.Series(dtype="object") for column in _OHLCV_COLUMNS})
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame = frame.loc[:, _OHLCV_COLUMNS]
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    def get_universe(self, d: date) -> UniverseSnapshot:
        """读取给定日期的 universe 快照。"""
        path = self._universe_path(d)
        if not path.exists():
            raise DataNotReady({"_universe": [d]})
        frame = ds.dataset(path.parent, format="parquet", partitioning="hive").to_table().to_pandas()
        excluded = {
            str(symbol): str(reason)
            for symbol, reason in zip(frame["excluded_symbol"], frame["reason_code"], strict=False)
            if pd.notna(symbol) and pd.notna(reason)
        }
        symbols = [str(symbol) for symbol in frame["symbol"].dropna().tolist()]
        return UniverseSnapshot(date=d, symbols=symbols, excluded=excluded)

    def is_trading_day(self, d: date) -> bool:
        """代理到底层 ``TradingCalendar``。"""
        return self._calendar.is_trading_day(d)

    def refresh_daily(self, d: date) -> RefreshResult:
        """刷新单日 universe 和 OHLCV 缓存；已成功过则直接命中缓存。"""
        started = time.monotonic()
        if not self._calendar.is_trading_day(d):
            return RefreshResult(target_date=d, skipped_non_trading_day=True)

        self._ensure_storage()
        cached_rows = self._refresh_state_rows(d)
        if cached_rows is not None:
            return RefreshResult(
                target_date=d,
                requested=cached_rows,
                cached_hit=cached_rows,
                quality_passed=True,
                duration_s=time.monotonic() - started,
            )

        snapshot = self._universe_manager.build_snapshot(d)
        self._write_universe(snapshot)

        frames: list[pd.DataFrame] = []
        failed = 0
        requested = len(snapshot.symbols)
        timestamp = self._now_iso()

        for symbol in snapshot.symbols:
            try:
                frame = self._gateway.fetch_ohlcv(symbol, d, d).copy()
            except FetchError as exc:
                failed += 1
                self._insert_fetch_log(timestamp, "ohlcv", symbol, d, "failed", exc.message)
                self._insert_fetch_error(timestamp, symbol, "ohlcv", exc.reason_code, exc.message)
                continue

            frame["symbol"] = symbol
            frame["date"] = pd.to_datetime(frame["date"]).dt.date
            current_frame = frame.loc[:, _OHLCV_COLUMNS].copy()
            frames.append(current_frame)
            self._insert_fetch_log(timestamp, "ohlcv", symbol, d, "ok", None)

        combined = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame({column: pd.Series(dtype="object") for column in _OHLCV_COLUMNS})
        )

        try:
            checks = self._quality_gate.check(combined)
        except QualityCheckFailed as exc:
            self._insert_data_quality_log(d, False, exc.checks)
            return RefreshResult(
                target_date=d,
                requested=requested,
                fetched=len(combined),
                failed=failed,
                quality_passed=False,
                duration_s=time.monotonic() - started,
            )

        self._insert_data_quality_log(d, True, checks)
        self._write_ohlcv(d, combined)
        self._upsert_refresh_state(d, "ok", len(combined))
        return RefreshResult(
            target_date=d,
            requested=requested,
            fetched=len(combined),
            failed=failed,
            quality_passed=True,
            duration_s=time.monotonic() - started,
        )

    def bootstrap_history(self, lookback_days: int, progress_cb=None) -> None:
        """按近 ``lookback_days`` 个交易日倒序回填，并支持断点续传。"""
        end = date.today()
        start = end - timedelta(days=max(lookback_days * 3, lookback_days))
        trading_days = self._calendar.trading_days_between(start, end)
        if len(trading_days) > lookback_days:
            trading_days = trading_days[-lookback_days:]
        trading_days = list(reversed(trading_days))
        total = len(trading_days)

        for index, current_day in enumerate(trading_days, start=1):
            if self._refresh_state_rows(current_day) is None:
                self.refresh_daily(current_day)
            if progress_cb is not None:
                progress_cb(index, total)

    def quality_report(self) -> DataHealth:
        """汇总最新 refresh 状态、今日覆盖率和错误积压。"""
        if not self._meta_db_path.exists():
            return DataHealth()

        with sqlite3.connect(self._meta_db_path) as conn:
            row = conn.execute(
                "SELECT ts FROM refresh_state WHERE status = 'ok' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            last_full_refresh = datetime.fromisoformat(row[0]) if row else None
            pending_retries = conn.execute(
                "SELECT COUNT(*) FROM fetch_errors WHERE resolved = 0"
            ).fetchone()[0]
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            unresolved_errors_24h = conn.execute(
                "SELECT COUNT(*) FROM fetch_errors WHERE resolved = 0 AND ts >= ?",
                (cutoff,),
            ).fetchone()[0]

        today = date.today()
        universe_size_today = 0
        ohlcv_coverage_today = 0.0
        try:
            universe = self.get_universe(today)
            universe_size_today = len(universe.symbols)
        except DataNotReady:
            universe = None

        ohlcv_path = self._ohlcv_path(today)
        if universe_size_today > 0 and ohlcv_path.exists():
            rows_today = len(
                ds.dataset(ohlcv_path.parent, format="parquet", partitioning="hive").to_table().to_pandas()
            )
            ohlcv_coverage_today = rows_today / universe_size_today

        health = "FAILED"
        if last_full_refresh is not None and universe_size_today > 0:
            if last_full_refresh.date() == today and ohlcv_coverage_today > 0.95:
                health = "OK"
            elif ohlcv_coverage_today > 0:
                health = "DEGRADED"

        return DataHealth(
            last_full_refresh=last_full_refresh,
            universe_size_today=universe_size_today,
            ohlcv_coverage_today=ohlcv_coverage_today,
            financials_freshness_days=-1,
            pending_retries=pending_retries,
            unresolved_errors_24h=unresolved_errors_24h,
            health=health,
        )

    def pending_retries(self) -> int:
        """返回未解决的抓取错误数量。"""
        if not self._meta_db_path.exists():
            return 0
        with sqlite3.connect(self._meta_db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM fetch_errors WHERE resolved = 0"
            ).fetchone()[0]

    def _ensure_storage(self) -> None:
        self._ohlcv_dir.mkdir(parents=True, exist_ok=True)
        self._universe_dir.mkdir(parents=True, exist_ok=True)
        self._calendar_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._meta_db_path) as conn:
            conn.execute(_FETCH_LOG_SCHEMA)
            conn.execute(_FETCH_ERRORS_SCHEMA)
            conn.execute(_DATA_QUALITY_LOG_SCHEMA)
            conn.execute(_REFRESH_STATE_SCHEMA)
            conn.commit()

    def _refresh_state_rows(self, d: date) -> int | None:
        if not self._meta_db_path.exists():
            return None
        with sqlite3.connect(self._meta_db_path) as conn:
            row = conn.execute(
                "SELECT rows FROM refresh_state WHERE target_date = ? AND status = 'ok'",
                (d.isoformat(),),
            ).fetchone()
        return None if row is None else int(row[0])

    def _missing_ohlcv(self, symbols: list[str], trading_days: list[date]) -> dict[str, list[date]]:
        if not trading_days:
            return {}

        missing_by_symbol = {symbol: [] for symbol in symbols}
        for current_day in trading_days:
            path = self._ohlcv_path(current_day)
            if not path.exists():
                for symbol in symbols:
                    missing_by_symbol[symbol].append(current_day)
                continue

            frame = (
                ds.dataset(path.parent, format="parquet", partitioning="hive")
                .to_table(columns=["symbol"])
                .to_pandas()
            )
            present = set(frame["symbol"].astype(str).tolist())
            for symbol in symbols:
                if symbol not in present:
                    missing_by_symbol[symbol].append(current_day)

        return {symbol: days for symbol, days in missing_by_symbol.items() if days}

    def _write_universe(self, snapshot: UniverseSnapshot) -> None:
        path = self._universe_path(snapshot.date)
        path.parent.mkdir(parents=True, exist_ok=True)
        row_count = max(len(snapshot.symbols), len(snapshot.excluded), 1)
        symbols = snapshot.symbols + [None] * (row_count - len(snapshot.symbols))
        excluded_items = list(snapshot.excluded.items())
        excluded_symbols = [item[0] for item in excluded_items] + [None] * (row_count - len(excluded_items))
        reason_codes = [item[1] for item in excluded_items] + [None] * (row_count - len(excluded_items))
        frame = pd.DataFrame(
            {
                "symbol": symbols,
                "excluded_symbol": excluded_symbols,
                "reason_code": reason_codes,
            }
        )
        payload = frame.drop(columns=["date"], errors="ignore")
        pq.write_table(pa.Table.from_pandas(payload, preserve_index=False), path)

    def _write_ohlcv(self, d: date, frame: pd.DataFrame) -> None:
        path = self._ohlcv_path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = frame.drop(columns=["date"], errors="ignore")
        pq.write_table(pa.Table.from_pandas(payload, preserve_index=False), path)

    def _insert_fetch_log(
        self,
        ts: str,
        endpoint: str,
        symbol: str | None,
        target_date: date | None,
        status: str,
        message: str | None,
    ) -> None:
        with sqlite3.connect(self._meta_db_path) as conn:
            conn.execute(
                "INSERT INTO fetch_log (ts, endpoint, symbol, target_date, status, message) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, endpoint, symbol, None if target_date is None else target_date.isoformat(), status, message),
            )
            conn.commit()

    def _insert_fetch_error(
        self,
        ts: str,
        symbol: str | None,
        endpoint: str,
        reason_code: str,
        message: str,
    ) -> None:
        with sqlite3.connect(self._meta_db_path) as conn:
            conn.execute(
                "INSERT INTO fetch_errors (ts, symbol, endpoint, reason_code, message, retry_count, resolved) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (ts, symbol, endpoint, reason_code, message),
            )
            conn.commit()

    def _insert_data_quality_log(self, target_date: date, passed: bool, checks: dict[str, bool]) -> None:
        with sqlite3.connect(self._meta_db_path) as conn:
            conn.execute(
                "INSERT INTO data_quality_log (ts, target_date, passed, checks_json) VALUES (?, ?, ?, ?)",
                (self._now_iso(), target_date.isoformat(), int(passed), json.dumps(checks, sort_keys=True)),
            )
            conn.commit()

    def _upsert_refresh_state(self, target_date: date, status: str, rows: int) -> None:
        with sqlite3.connect(self._meta_db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO refresh_state (target_date, ts, status, rows) VALUES (?, ?, ?, ?)",
                (target_date.isoformat(), self._now_iso(), status, rows),
            )
            conn.commit()

    def _ohlcv_path(self, d: date) -> Path:
        return self._ohlcv_dir / f"date={d.isoformat()}" / "part.parquet"

    def _universe_path(self, d: date) -> Path:
        return self._universe_dir / f"date={d.isoformat()}" / "snap.parquet"

    def _now_iso(self) -> str:
        return datetime.now().isoformat()
