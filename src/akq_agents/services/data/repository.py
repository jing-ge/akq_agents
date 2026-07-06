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

# M27: refresh_daily 每日除了拉 universe 里的个股, 还要拉这些 benchmark 指数,
# 用于 portfolio_nav.benchmark_nav 计算和净值图对比. 指数不在 universe 里所以主循环拉不到,
# refresh_daily 里单独走 gateway.fetch_ohlcv (内部会走 stock_zh_index_daily 分支).
_BENCHMARK_INDICES = ["000300"]  # 沪深 300, 未来需要中证 500 (000905) 等再加


def open_meta_db(path: Path) -> sqlite3.Connection:
    """打开 ``meta.db`` 连接并应用 P1 附录 B §6 承诺的 PRAGMA。

    供 P2/P3/P4 等后续阶段共用。WAL 是 db 文件级 sticky 属性、写一次即生效；
    ``busy_timeout`` 是 connection 级属性、每次连接都必须重新 set。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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

    def get_ohlcv_loose(self, symbols, start: date, end: date) -> pd.DataFrame:
        """宽容读 OHLCV：直接扫 parquet 区间，缺哪天就缺哪天，不抛 DataNotReady。

        统一替代 portfolio_agent / discovery / batch_deep_research 里的 3 处重复实现。
        """
        if not self._ohlcv_dir.exists() or not symbols:
            return pd.DataFrame()
        dataset = ds.dataset(self._ohlcv_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(list(symbols)),
        )
        frame = table.to_pandas()
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    def is_trading_day(self, d: date) -> bool:
        """代理到底层 ``TradingCalendar``。"""
        return self._calendar.is_trading_day(d)

    def refresh_daily(self, d: date) -> RefreshResult:
        """刷新单日 universe 和 OHLCV 缓存；已成功过则直接命中缓存。

        Universe 阶段任何异常（``FetchError`` / 网络 / akshare 内部问题）都会被
        catch 住、写入 ``fetch_errors`` 并返回 ``RefreshResult(failed=...)``，
        本方法对调用方保证"永不抛出"，便于长跑任务安全续。
        """
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

        try:
            snapshot = self._universe_manager.build_snapshot(d)
        except Exception as exc:  # noqa: BLE001 — long-running job must not crash on single-day failure
            reason_code = getattr(exc, "reason_code", "UNKNOWN")
            message = getattr(exc, "message", None) or str(exc)
            self._insert_fetch_error(self._now_iso(), None, "universe", reason_code, message)
            return RefreshResult(
                target_date=d,
                requested=0,
                failed=0,
                quality_passed=False,
                duration_s=time.monotonic() - started,
            )
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

        # M27: 单独拉指数 (000300 沪深 300 等 benchmark), 不在 universe 里所以主循环拉不到.
        # 用 gateway.fetch_ohlcv 走同一 API 契约, gateway 内部走 stock_zh_index_daily 分支.
        # 拉失败不影响主流程 (仅 benchmark_nav 缺失), 只 log 一条 error 而已.
        for index_sym in _BENCHMARK_INDICES:
            try:
                frame = self._gateway.fetch_ohlcv(index_sym, d, d).copy()
            except Exception as exc:  # noqa: BLE001
                self._insert_fetch_log(timestamp, "ohlcv", index_sym, d, "failed", str(exc)[:200])
                self._insert_fetch_error(
                    timestamp, index_sym, "ohlcv",
                    getattr(exc, "reason_code", "UNKNOWN"),
                    getattr(exc, "message", None) or str(exc)[:200],
                )
                continue
            if frame is None or frame.empty:
                # 指数拉到但当天数据缺 (可能是非交易日边界或数据延迟), 不算失败
                continue
            frame["symbol"] = index_sym
            frame["date"] = pd.to_datetime(frame["date"]).dt.date
            frames.append(frame.loc[:, _OHLCV_COLUMNS].copy())
            self._insert_fetch_log(timestamp, "ohlcv", index_sym, d, "ok", None)

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

    def refresh_daily_fast(self, d: date) -> RefreshResult:
        """⚡ 快速增量刷新：用 ``fetch_market_snapshot_today`` 一次性拉全市场快照。

        - 仅对**今日**有效（snapshot 接口只给当天数据）
        - 跳过 universe 重建（如有需要外部独立刷 universe）
        - 一次 HTTP 调用 vs 4500+ 次单股调用，从 30 分钟降到 ~15 秒
        - 网络错误会抛出 ``FetchError``（不像 refresh_daily 那样 swallow）
        """
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

        # 1) universe：若当日 universe 缺失，先拿一个最近可用的（不强制 rebuild）
        try:
            _ = self.get_universe(d)
        except DataNotReady:
            # 用今日 snapshot 自己构造 universe（这天有数据的 symbol 集合）
            pass

        # 2) 拉快照
        try:
            snapshot_df = self._gateway.fetch_market_snapshot_today()
        except FetchError as exc:
            self._insert_fetch_error(
                self._now_iso(), None, "ohlcv_batch", exc.reason_code, str(exc)
            )
            return RefreshResult(
                target_date=d,
                requested=0,
                failed=1,
                quality_passed=False,
                duration_s=time.monotonic() - started,
            )

        if snapshot_df.empty:
            return RefreshResult(
                target_date=d,
                requested=0,
                failed=1,
                quality_passed=False,
                duration_s=time.monotonic() - started,
            )

        # 3) 写 parquet
        # snapshot_df 列：symbol, open, high, low, close, volume, amount
        # _write_ohlcv 不要求 date 列（路径里已带 hive 分区 date=...）
        self._write_ohlcv(d, snapshot_df)

        # 4) 如果当日 universe 缺失，用 snapshot 里的 symbol 集合写一份最小 universe
        if not self._universe_path(d).exists():
            from akq_agents.services.data.schemas import UniverseSnapshot

            self._write_universe(UniverseSnapshot(
                date=d,
                symbols=sorted(snapshot_df["symbol"].astype(str).unique().tolist()),
                excluded={},
            ))

        # 5) 质量门 + refresh_state
        n_rows = len(snapshot_df)
        passed = n_rows >= 3000  # 简单门槛：A 股不应少于 3000 只
        self._insert_data_quality_log(d, passed, {"row_count": passed})
        self._upsert_refresh_state(d, "ok" if passed else "partial", n_rows)

        return RefreshResult(
            target_date=d,
            requested=n_rows,
            fetched=n_rows,
            quality_passed=passed,
            duration_s=time.monotonic() - started,
        )

    def bootstrap_history(self, lookback_days: int, progress_cb=None) -> None:
        """按 symbol 模式回填近 ``lookback_days`` 个交易日历史。

        与 ``refresh_daily`` 的"按天"循环互补：本方法对每只 symbol 一次性调用
        ``gateway.fetch_ohlcv(symbol, start, end)`` 把整段 2 年数据拿回来，
        再按日切分写入 Parquet 分区。这避免了"每天对全市场每只股发一次请求"
        造成的 N×M 接口压力。

        步骤：
        1. 取最新 universe（一次 spot 调用）作为待回填的 symbol 全集。
        2. 决定回填日历窗口 ``[start, end]``。
        3. 逐 symbol 拉一段 OHLCV → 缓存进 ``self._symbol_buffer``。
        4. 全部 symbol 处理完后按日切片，每日调用一次 ``_finalize_day(...)``
           做质量门 + 落 parquet + 写 refresh_state。
        5. 单 symbol 失败 → 写 fetch_errors 继续；单日质量失败 → 写
           data_quality_log 但不阻塞其它日；**整个过程绝不抛**。

        ``progress_cb(done, total, status)``：每完成一个 symbol 回调一次，
        ``status`` ∈ ``{"ok", "skipped"}``。
        """
        self._ensure_storage()
        end = date.today()
        # 用足够长的窗口让 calendar 选出 lookback_days 个交易日
        start_window = end - timedelta(days=max(lookback_days * 3, lookback_days))
        trading_days = self._calendar.trading_days_between(start_window, end)
        if len(trading_days) > lookback_days:
            trading_days = trading_days[-lookback_days:]
        if not trading_days:
            return
        target_start = trading_days[0]
        target_end = trading_days[-1]
        trading_day_set = set(trading_days)

        # 取 universe（一次 spot 调用）
        snapshot_date = end
        try:
            snapshot = self._universe_manager.build_snapshot(snapshot_date)
        except Exception as exc:  # noqa: BLE001
            reason_code = getattr(exc, "reason_code", "UNKNOWN")
            message = getattr(exc, "message", None) or str(exc)
            self._insert_fetch_error(self._now_iso(), None, "universe", reason_code, message)
            return
        self._write_universe(snapshot)

        # 逐 symbol 拉数据，按日聚合到 buffer
        # 结构：{date_iso: list[pd.DataFrame(rows for that day)]}
        day_buffer: dict[str, list[pd.DataFrame]] = {}
        total = len(snapshot.symbols)
        for index, symbol in enumerate(snapshot.symbols, start=1):
            status = "ok"
            try:
                frame = self._gateway.fetch_ohlcv(symbol, target_start, target_end).copy()
            except Exception as exc:  # noqa: BLE001
                status = "skipped"
                reason_code = getattr(exc, "reason_code", "UNKNOWN")
                message = getattr(exc, "message", None) or str(exc)
                self._insert_fetch_log(self._now_iso(), "ohlcv", symbol, target_end, "failed", message)
                self._insert_fetch_error(self._now_iso(), symbol, "ohlcv", reason_code, message)
            else:
                frame["symbol"] = symbol
                frame["date"] = pd.to_datetime(frame["date"]).dt.date
                # 只保留交易日（防 akshare 返回非交易日）
                frame = frame[frame["date"].isin(trading_day_set)]
                if not frame.empty:
                    sub = frame.loc[:, _OHLCV_COLUMNS].copy()
                    for day_iso, daily_frame in sub.groupby(sub["date"].map(lambda x: x.isoformat())):
                        day_buffer.setdefault(str(day_iso), []).append(daily_frame)
                self._insert_fetch_log(self._now_iso(), "ohlcv", symbol, target_end, "ok", None)

            if progress_cb is not None:
                progress_cb(index, total, status)

        # 按日落盘
        for day_iso, frames in day_buffer.items():
            d = date.fromisoformat(day_iso)
            if self._refresh_state_rows(d) is not None:
                continue
            combined = pd.concat(frames, ignore_index=True)
            try:
                checks = self._quality_gate.check(combined)
            except QualityCheckFailed as exc:
                self._insert_data_quality_log(d, False, exc.checks)
                continue
            self._insert_data_quality_log(d, True, checks)
            self._write_ohlcv(d, combined)
            self._upsert_refresh_state(d, "ok", len(combined))

    def quality_report(self) -> DataHealth:
        """汇总最新 refresh 状态、今日覆盖率和错误积压。"""
        if not self._meta_db_path.exists():
            return DataHealth()

        with open_meta_db(self._meta_db_path) as conn:
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
        elif universe_size_today == 0 and self._calendar.is_trading_day(today) and datetime.now().hour < 16:
            # 交易日 16:00（data.refresh_daily 首次 cron 时刻，见 SchedulerConfig.first_try_hour）
            # 前数据还没刷是预期状态，不要报 FAILED 误导下游。
            health = "PENDING_TODAY"

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
        with open_meta_db(self._meta_db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM fetch_errors WHERE resolved = 0"
            ).fetchone()[0]

    def _ensure_storage(self) -> None:
        self._ohlcv_dir.mkdir(parents=True, exist_ok=True)
        self._universe_dir.mkdir(parents=True, exist_ok=True)
        self._calendar_path.parent.mkdir(parents=True, exist_ok=True)
        with open_meta_db(self._meta_db_path) as conn:
            # P1 附录 B §6：强制 WAL + busy_timeout=5000，给多进程并发（daemon 写 / web 读 / LLM tools 读）兜底
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_FETCH_LOG_SCHEMA)
            conn.execute(_FETCH_ERRORS_SCHEMA)
            conn.execute(_DATA_QUALITY_LOG_SCHEMA)
            conn.execute(_REFRESH_STATE_SCHEMA)
            conn.commit()

    def _refresh_state_rows(self, d: date) -> int | None:
        if not self._meta_db_path.exists():
            return None
        with open_meta_db(self._meta_db_path) as conn:
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
        with open_meta_db(self._meta_db_path) as conn:
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
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                "INSERT INTO fetch_errors (ts, symbol, endpoint, reason_code, message, retry_count, resolved) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (ts, symbol, endpoint, reason_code, message),
            )
            conn.commit()

    def _insert_data_quality_log(self, target_date: date, passed: bool, checks: dict[str, bool]) -> None:
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                "INSERT INTO data_quality_log (ts, target_date, passed, checks_json) VALUES (?, ?, ?, ?)",
                (self._now_iso(), target_date.isoformat(), int(passed), json.dumps(checks, sort_keys=True)),
            )
            conn.commit()

    def _upsert_refresh_state(self, target_date: date, status: str, rows: int) -> None:
        with open_meta_db(self._meta_db_path) as conn:
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
