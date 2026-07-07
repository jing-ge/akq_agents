"""BoardRepository：行业板块行情快照的每日落地与读取。

设计完全照抄 :class:`DataRepository` 的既有模式，长在同一套存储骨架上：
- Parquet 按天 hive 分区：``data/parquet/board/date=YYYY-MM-DD/snap.parquet``
- meta.db 记录每日刷新状态：``board_refresh_state`` 表（照抄 ``refresh_state``）
- 原子写（临时文件 + os.replace），复用 ``open_meta_db`` 的 PRAGMA

数据源：``gateway.fetch_board_snapshot()``（同花顺行业板块，一次调用给齐
涨跌幅 / 成交额 / 资金净流入 / 涨跌家数 / 领涨股）。板块接口只给**当日**快照，
故历史从上线当天起每日累积。

第一版只做**行业**板块（概念板块 akshare 聚合行情在本地网络拿不到）。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from akq_agents.services.data.exceptions import DataNotReady
from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)

_BOARD_REFRESH_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_refresh_state (
  target_date TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  status TEXT NOT NULL,
  rows INTEGER NOT NULL
);
"""

# 落盘列（date 由 hive 分区路径承载，不写进文件）
_BOARD_FILE_COLUMNS = [
    "board_name", "pct_chg", "amount", "net_inflow",
    "up_count", "down_count", "leader_name", "leader_pct",
]
_BOARD_COLUMNS = ["date"] + _BOARD_FILE_COLUMNS

# 质量门：A 股行业板块不应少于这个数（同花顺现约 90 个）
_MIN_BOARDS = 50


class BoardRefreshResult:
    """轻量返回对象（不引入新 pydantic schema，够用即可）。"""

    def __init__(
        self,
        target_date: date,
        rows: int = 0,
        cached_hit: int = 0,
        skipped_non_trading_day: bool = False,
        quality_passed: bool = False,
        duration_s: float = 0.0,
    ) -> None:
        self.target_date = target_date
        self.rows = rows
        self.cached_hit = cached_hit
        self.skipped_non_trading_day = skipped_non_trading_day
        self.quality_passed = quality_passed
        self.duration_s = duration_s

    def as_dict(self) -> dict:
        return {
            "target_date": self.target_date.isoformat(),
            "rows": self.rows,
            "cached_hit": self.cached_hit,
            "skipped_non_trading_day": self.skipped_non_trading_day,
            "quality_passed": self.quality_passed,
            "duration_s": self.duration_s,
        }


class BoardRepository:
    """行业板块快照的读写入口。复用 DataRepository 的 base_dir / meta.db / 日历。"""

    def __init__(self, gateway, calendar, base_dir: Path, meta_db_path: Path) -> None:
        """
        Args:
            gateway: AKShareGateway（需有 ``fetch_board_snapshot``）
            calendar: TradingCalendar（``is_trading_day`` / ``trading_days_between``）
            base_dir: 数据根目录（与 DataRepository 同一个 ``data/``）
            meta_db_path: meta.db 路径（复用 ``repo.meta_db_path``）
        """
        self._gateway = gateway
        self._calendar = calendar
        self._base_dir = Path(base_dir)
        self._board_dir = self._base_dir / "parquet" / "board"
        self._meta_db_path = Path(meta_db_path)

    # ----------------- 写 -----------------

    def refresh_board_daily(self, d: date) -> BoardRefreshResult:
        """刷新单日行业板块快照；已成功过则命中缓存直接返回。

        照抄 ``DataRepository.refresh_daily`` 的契约：非交易日 skip、缓存命中 skip、
        质量门不过则不写 refresh_state。网络类异常会抛 ``FetchError``（让 job 层
        写 fetch_errors + alerter 可见，和 data.refresh_daily 一致）。
        """
        started = time.monotonic()
        if not self._calendar.is_trading_day(d):
            return BoardRefreshResult(target_date=d, skipped_non_trading_day=True)

        self._ensure_storage()
        cached_rows = self._refresh_state_rows(d)
        if cached_rows is not None:
            return BoardRefreshResult(
                target_date=d, rows=cached_rows, cached_hit=cached_rows,
                quality_passed=True, duration_s=time.monotonic() - started,
            )

        # 拉当日快照（板块接口只给当天，target 必须是今天才有意义）
        frame = self._gateway.fetch_board_snapshot()  # 网络失败 → FetchError 上抛
        n_rows = len(frame)
        passed = n_rows >= _MIN_BOARDS
        if not passed:
            logger.warning(
                "board.refresh_daily: quality FAILED target=%s rows=%d (< %d)",
                d.isoformat(), n_rows, _MIN_BOARDS,
            )
            return BoardRefreshResult(
                target_date=d, rows=n_rows, quality_passed=False,
                duration_s=time.monotonic() - started,
            )

        self._write_board(d, frame)
        self._upsert_refresh_state(d, "ok", n_rows)
        return BoardRefreshResult(
            target_date=d, rows=n_rows, quality_passed=True,
            duration_s=time.monotonic() - started,
        )

    def backfill_history(self, lookback_days: int = 30, progress_cb=None) -> dict:
        """回填近 ``lookback_days`` 个交易日的板块历史涨跌幅（真实数据）。

        数据源 ``gateway.fetch_board_hist``（同花顺行业指数日线，自算涨跌幅）。
        步骤：
        1. 用当日快照的板块名列表作为待回填全集（若当日无快照，先拉一次）。
        2. 逐板块拉 ``[start, end]`` 历史 → 缓存进按日 buffer。
        3. 按日落地：某天已有 ok 的 refresh_state（如当日完整快照）→ **跳过**，
           不用「只有 pct_chg」的历史行覆盖字段齐全的当日行。
        4. 单板块失败不阻塞；整个过程不抛（返回统计）。

        历史行只有 ``pct_chg`` 有值，其余快照字段（amount/leader…）填空 —— 热力图
        只用 pct_chg，排行榜看当日快照，互不影响。列 schema 与当日快照保持一致。

        ``progress_cb(done, total)`` 每完成一个板块回调一次。
        """
        self._ensure_storage()

        # 1) 板块名全集：优先当日快照，缺则现抓一次
        end = date.today()
        try:
            boards = self.get_board_snapshot(end)["board_name"].tolist()
        except DataNotReady:
            snap = self._gateway.fetch_board_snapshot()
            boards = snap["board_name"].astype(str).tolist()

        # 窗口：日历日给宽（lookback*2），并前置 1 周让首日有 pct_chg 前值
        start = end - timedelta(days=lookback_days * 2 + 7)

        # 2) 逐板块拉，聚到按日 buffer：{date: [ {board_name,pct_chg}, ... ]}
        day_buffer: dict[date, list[dict]] = {}
        total = len(boards)
        ok_boards = 0
        failed_boards = 0
        for i, board in enumerate(boards, start=1):
            try:
                hist = self._gateway.fetch_board_hist(board, start, end)
            except Exception as exc:  # noqa: BLE001 — 单板块失败不阻塞整体回填
                failed_boards += 1
                logger.warning("board.backfill: %s failed: %s", board, exc)
            else:
                ok_boards += 1
                for _, row in hist.iterrows():
                    day_buffer.setdefault(row["date"], []).append(
                        {"board_name": row["board_name"], "pct_chg": float(row["pct_chg"])}
                    )
            if progress_cb is not None:
                progress_cb(i, total)

        # 3) 只保留窗口内最近 lookback_days 个已落地交易日
        all_days = sorted(day_buffer.keys())
        if len(all_days) > lookback_days:
            all_days = all_days[-lookback_days:]

        # 4) 按日落地（跳过已有 ok 的天，避免覆盖当日完整快照）
        written_days = 0
        for d in all_days:
            if self._refresh_state_rows(d) is not None:
                continue
            records = day_buffer[d]
            if len(records) < _MIN_BOARDS:
                continue
            frame = pd.DataFrame(records)
            # 补齐当日快照 schema 缺列（历史只有 pct_chg）。类型必须与当日快照一致：
            # 数值列 → float(NaN)（当日快照是 double），名称列 → ""（object），
            # 否则整列 pd.NA 会被 pyarrow 推断成 null 类型，与 double 分区跨天扫描时
            # 报 ArrowNotImplementedError: Unsupported cast from double to null。
            _numeric_cols = {"pct_chg", "amount", "net_inflow", "up_count", "down_count", "leader_pct"}
            for col in _BOARD_FILE_COLUMNS:
                if col not in frame.columns:
                    if col in _numeric_cols:
                        frame[col] = pd.Series([float("nan")] * len(frame), dtype="float64")
                    else:
                        frame[col] = pd.Series([""] * len(frame), dtype="object")
            # pct_chg 也强制 float64（防 records 里混入 int）
            frame["pct_chg"] = frame["pct_chg"].astype("float64")
            frame = frame.loc[:, _BOARD_FILE_COLUMNS]
            self._write_board(d, frame)
            self._upsert_refresh_state(d, "ok", len(frame))
            written_days += 1

        result = {
            "boards_total": total,
            "boards_ok": ok_boards,
            "boards_failed": failed_boards,
            "days_written": written_days,
            "days_available": len(self.available_dates(limit=lookback_days + 5)),
        }
        logger.info("board.backfill done: %s", result)
        return result

    # ----------------- 读 -----------------

    def get_board_snapshot(self, d: date) -> pd.DataFrame:
        """读单日板块快照；缺失抛 ``DataNotReady``。"""
        path = self._board_path(d)
        if not path.exists():
            raise DataNotReady({"_board": [d]})
        frame = ds.dataset(path.parent, format="parquet", partitioning="hive").to_table().to_pandas()
        # hive 分区会带 date 列；补齐并规整
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"]).dt.date
        else:
            frame["date"] = d
        return frame.loc[:, _BOARD_COLUMNS].sort_values("pct_chg", ascending=False).reset_index(drop=True)

    def get_board_history(self, start: date, end: date) -> pd.DataFrame:
        """读区间内所有已落地的板块快照（供轮动热力图）；缺哪天缺哪天，不抛。"""
        if not self._board_dir.exists():
            return pd.DataFrame(columns=_BOARD_COLUMNS)
        dataset = ds.dataset(self._board_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat()) & (ds.field("date") <= end.isoformat())
        )
        frame = table.to_pandas()
        if frame.empty:
            return pd.DataFrame(columns=_BOARD_COLUMNS)
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame.loc[:, _BOARD_COLUMNS].sort_values(["date", "pct_chg"]).reset_index(drop=True)

    def available_dates(self, limit: int = 30) -> list[date]:
        """已落地板块数据的日期列表（新→旧），用于前端日期选择/热力图窗口。"""
        if not self._meta_db_path.exists():
            return []
        with open_meta_db(self._meta_db_path) as conn:
            rows = conn.execute(
                "SELECT target_date FROM board_refresh_state WHERE status = 'ok' "
                "ORDER BY target_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [date.fromisoformat(r[0]) for r in rows]

    # ----------------- internal -----------------

    def _ensure_storage(self) -> None:
        self._board_dir.mkdir(parents=True, exist_ok=True)
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(_BOARD_REFRESH_STATE_SCHEMA)
            conn.commit()

    def _refresh_state_rows(self, d: date) -> int | None:
        if not self._meta_db_path.exists():
            return None
        with open_meta_db(self._meta_db_path) as conn:
            row = conn.execute(
                "SELECT rows FROM board_refresh_state WHERE target_date = ? AND status = 'ok'",
                (d.isoformat(),),
            ).fetchone()
        return None if row is None else int(row[0])

    def _upsert_refresh_state(self, d: date, status: str, rows: int) -> None:
        with open_meta_db(self._meta_db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO board_refresh_state (target_date, ts, status, rows) "
                "VALUES (?, ?, ?, ?)",
                (d.isoformat(), datetime.now().isoformat(), status, rows),
            )
            conn.commit()

    def _write_board(self, d: date, frame: pd.DataFrame) -> None:
        path = self._board_path(d)
        payload = frame.drop(columns=["date"], errors="ignore")
        self._atomic_write_table(pa.Table.from_pandas(payload, preserve_index=False), path)

    def _atomic_write_table(self, table: pa.Table, path: Path) -> None:
        """原子写：临时文件 + os.replace（照抄 DataRepository._atomic_write_table）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _board_path(self, d: date) -> Path:
        return self._board_dir / f"date={d.isoformat()}" / "snap.parquet"
