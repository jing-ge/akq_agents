"""板块看板 API（``/api/board/*``）。

第一版数据源：同花顺行业板块（``BoardRepository``）。提供：
- ``GET  /api/board/ranking``  今日（或指定日）板块涨跌幅排行榜
- ``GET  /api/board/heatmap``  近 N 日板块每日涨跌幅矩阵（轮动热力图）
- ``POST /api/board/refresh``  立即抓取当日快照并落地（无 daemon job 时手动触发用）

``BoardRepository`` 从 ``svc.repo`` 轻量构造（复用其 gateway / calendar / 路径），
不额外挂进 ServiceContainer，保持第一版改动面最小。
"""

from __future__ import annotations

import logging
import threading
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from akq_agents.services.data.board_repository import BoardRepository
from akq_agents.services.data.exceptions import DataNotReady, FetchError
from akq_agents.web.deps import get_services

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_board_repo() -> BoardRepository:
    """从 svc.repo 复用 gateway / calendar / 路径构造 BoardRepository。"""
    svc = get_services()
    repo = svc.repo
    if repo is None:
        raise HTTPException(status_code=503, detail="data repository 未就绪")
    return BoardRepository(
        gateway=repo._gateway,
        calendar=repo._calendar,
        base_dir=repo._base_dir,
        meta_db_path=repo.meta_db_path,
    )


def _parse_date(s: str | None) -> _date:
    if not s:
        return _date.today()
    try:
        return _date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD: {s}") from exc


@router.get("/ranking")
async def board_ranking(
    date: str | None = Query(default=None, description="YYYY-MM-DD，默认最新可用日"),
    top: int = Query(default=0, ge=0, le=200, description="只取涨跌幅前后各 N（0=全部）"),
) -> dict[str, Any]:
    """板块涨跌幅排行榜。若指定日无数据，回退到最新已落地日。"""
    board_repo = _get_board_repo()
    target = _parse_date(date)

    # 指定日缺数据 → 回退最新可用日（第一版历史少，避免空屏）
    try:
        frame = board_repo.get_board_snapshot(target)
    except DataNotReady:
        available = board_repo.available_dates(limit=1)
        if not available:
            return {"date": target.isoformat(), "rows": [], "available": [],
                    "note": "暂无板块数据，请先点击『立即抓取』"}
        target = available[0]
        frame = board_repo.get_board_snapshot(target)

    if top > 0 and len(frame) > top * 2:
        frame = frame.iloc[list(range(top)) + list(range(len(frame) - top, len(frame)))]

    rows = [
        {
            "board_name": r["board_name"],
            "pct_chg": _num(r["pct_chg"]),
            "amount": _num(r["amount"]),
            "net_inflow": _num(r["net_inflow"]),
            "up_count": _int(r["up_count"]),
            "down_count": _int(r["down_count"]),
            "leader_name": r["leader_name"],
            "leader_pct": _num(r["leader_pct"]),
        }
        for _, r in frame.iterrows()
    ]
    return {
        "date": target.isoformat(),
        "rows": rows,
        "available": [d.isoformat() for d in board_repo.available_dates(limit=30)],
    }


@router.get("/heatmap")
async def board_heatmap(
    end: str | None = Query(default=None, description="窗口结束日 YYYY-MM-DD，默认今天"),
    lookback: int = Query(default=20, ge=2, le=120, description="回看交易日数"),
) -> dict[str, Any]:
    """近 N 日板块每日涨跌幅矩阵（板块 × 日期），用于轮动热力图。

    - 只用已落地的日期（历史从上线当天累积），窗口内缺的天自然不出现。
    - 板块按“窗口内平均涨跌幅”降序，让强势板块排上面。
    """
    board_repo = _get_board_repo()
    end_d = _parse_date(end)
    start_d = end_d - timedelta(days=lookback * 2)  # 日历日给宽，实际以落地数据为准

    hist = board_repo.get_board_history(start_d, end_d)
    if hist.empty:
        return {"dates": [], "boards": [], "cells": [],
                "note": "暂无板块历史，多攒几天或先『立即抓取』"}

    dates = sorted({d.isoformat() for d in hist["date"]})
    if len(dates) > lookback:
        dates = dates[-lookback:]
    hist = hist[hist["date"].map(lambda d: d.isoformat()).isin(dates)]

    # 板块按窗口内平均涨跌幅排序
    order = (
        hist.groupby("board_name")["pct_chg"].mean().sort_values(ascending=False).index.tolist()
    )
    date_idx = {d: i for i, d in enumerate(dates)}
    board_idx = {b: i for i, b in enumerate(order)}

    # ECharts heatmap cell: [x=date_idx, y=board_idx, value=pct_chg]
    cells = []
    for _, r in hist.iterrows():
        di = date_idx.get(r["date"].isoformat())
        bi = board_idx.get(r["board_name"])
        if di is None or bi is None:
            continue
        cells.append([di, bi, _num(r["pct_chg"])])

    return {"dates": dates, "boards": order, "cells": cells}


@router.post("/refresh")
async def board_refresh(
    date: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
) -> dict[str, Any]:
    """立即抓取当日板块快照并落地（无 daemon job 时手动触发）。

    注意：板块接口只给**当日**数据，指定过去日期只会命中已有缓存或失败。
    """
    board_repo = _get_board_repo()
    target = _parse_date(date)
    try:
        result = board_repo.refresh_board_daily(target)
    except FetchError as exc:
        logger.warning("board.refresh manual failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"抓取失败（{exc.reason_code}）：{exc.message}") from exc
    return result.as_dict()


# 回填是 ~20s 的长任务，用后台线程执行；前端轮询 _BACKFILL_STATUS 拿进度。
# 单进程单 worker（uvicorn --workers 1），模块级状态足够；加锁防并发重入。
_BACKFILL_STATUS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "result": None, "error": None}
_BACKFILL_LOCK = threading.Lock()


def _run_backfill(lookback: int) -> None:
    try:
        board_repo = _get_board_repo()

        def _cb(done: int, total: int) -> None:
            _BACKFILL_STATUS["done"] = done
            _BACKFILL_STATUS["total"] = total

        result = board_repo.backfill_history(lookback_days=lookback, progress_cb=_cb)
        _BACKFILL_STATUS["result"] = result
    except Exception as exc:  # noqa: BLE001 — 后台线程兜底，错误存进 status 供前端读
        logger.warning("board.backfill thread failed: %s", exc)
        _BACKFILL_STATUS["error"] = str(exc)
    finally:
        _BACKFILL_STATUS["running"] = False


@router.post("/backfill")
async def board_backfill(
    lookback: int = Query(default=30, ge=2, le=120, description="回填近 N 个交易日"),
) -> dict[str, Any]:
    """后台回填近 N 日**真实**板块历史（同花顺行业指数日线）。

    立即返回 ``started``；前端轮询 ``GET /api/board/backfill/status`` 看进度。
    ~90 个板块约 20 秒。已有完整快照的天不会被覆盖。
    """
    with _BACKFILL_LOCK:
        if _BACKFILL_STATUS["running"]:
            return {"started": False, "reason": "already_running", **_BACKFILL_STATUS}
        _BACKFILL_STATUS.update({"running": True, "done": 0, "total": 0, "result": None, "error": None})
        threading.Thread(target=_run_backfill, args=(lookback,), daemon=True).start()
    return {"started": True, "lookback": lookback}


@router.get("/backfill/status")
async def board_backfill_status() -> dict[str, Any]:
    """回填进度轮询。"""
    return dict(_BACKFILL_STATUS)


@router.get("/kline")
async def board_kline(
    board: str = Query(..., description="板块名，如 半导体"),
    lookback: int = Query(default=120, ge=20, le=500, description="回看日历日数"),
) -> dict[str, Any]:
    """单个板块的 OHLC 日 K（实时拉取，不落地）。返回 K 线 + MA5/10/20。"""
    svc = get_services()
    repo = svc.repo
    if repo is None:
        raise HTTPException(status_code=503, detail="data repository 未就绪")
    end = _date.today()
    start = end - timedelta(days=lookback)
    try:
        df = repo._gateway.fetch_board_kline(board, start, end)
    except FetchError as exc:
        logger.warning("board.kline %s failed: %s", board, exc)
        raise HTTPException(status_code=502, detail=f"K 线获取失败（{exc.reason_code}）") from exc
    if df.empty:
        return {"board": board, "dates": [], "kline": [], "ma5": [], "ma10": [], "ma20": []}

    closes = df["close"].tolist()

    def _ma(n: int) -> list:
        out = []
        for i in range(len(closes)):
            if i + 1 < n:
                out.append(None)
            else:
                out.append(round(sum(closes[i + 1 - n:i + 1]) / n, 2))
        return out

    return {
        "board": board,
        "dates": [d.isoformat() for d in df["date"]],
        # ECharts candlestick 顺序：[open, close, low, high]
        "kline": [[round(o, 2), round(c, 2), round(lo, 2), round(hi, 2)]
                  for o, c, lo, hi in zip(df["open"], df["close"], df["low"], df["high"], strict=False)],
        "ma5": _ma(5), "ma10": _ma(10), "ma20": _ma(20),
    }


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return None if f != f else round(f, 3)  # NaN → None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
