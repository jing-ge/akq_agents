"""Trading endpoints（P0-1）：实盘持仓 + 今日交易清单。"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any

from fastapi import APIRouter, HTTPException

from akq_agents.web.deps import get_services

logger = logging.getLogger(__name__)
router = APIRouter()


def _recompute_today_trade_list() -> dict[str, Any]:
    """holdings 改后立即重算今日 trade_list（基于最新 portfolio_snapshot）。

    返回：{recomputed: bool, n_items: int, error: str | None}
    """
    svc = get_services()
    workflow = svc.workflow
    if workflow is None:
        return {"recomputed": False, "error": "workflow not ready"}
    services = workflow.services
    snap_store = services.get("portfolio_snapshot_store")
    holdings_store = services.get("holdings_store")
    tl_store = services.get("trade_list_store")
    tl_cfg = services.get("trade_list_config")
    repo = services.get("data_repository")
    ind_store = services.get("industry_map_store")

    if not all([snap_store, holdings_store, tl_store, tl_cfg, repo]):
        return {"recomputed": False, "error": "missing services"}

    # 找最新有 snapshot 的日期
    snapshot_dates = snap_store.list_dates(limit=1)
    if not snapshot_dates:
        return {"recomputed": False, "error": "no snapshots"}
    target_date = _date.fromisoformat(snapshot_dates[0])

    # 读 snapshot → 权重 + 上一日权重
    rows = snap_store.read_snapshot(target_date)
    if not rows:
        return {"recomputed": False, "error": "no snapshot for today"}
    weights = {r.symbol: float(r.weight) for r in rows}
    composite = {r.symbol: float(r.composite_score or 0.0) for r in rows}
    # prev: 用 snapshot 表里上一日
    prev_weights_series = snap_store.read_prev_weights(target_date)
    prev_weights = {str(s): float(w) for s, w in prev_weights_series.items()} if not prev_weights_series.empty else {}

    # 拿当日 close（从 ohlcv parquet 查 weights 里的 symbol + holdings 里的 symbol）
    holdings_dict = holdings_store.as_dict()
    all_syms = set(weights.keys()) | set(holdings_dict.keys())
    import pyarrow.dataset as ds
    from datetime import timedelta
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    today_close: dict[str, float] = {}
    if ohlcv_dir and ohlcv_dir.exists() and all_syms:
        try:
            start = (target_date - timedelta(days=7)).isoformat()
            end = target_date.isoformat()
            dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
            table = dataset.to_table(
                filter=(ds.field("date") >= start)
                       & (ds.field("date") <= end)
                       & ds.field("symbol").isin(list(all_syms)),
                columns=["date", "symbol", "close"],
            )
            df = table.to_pandas()
            if not df.empty:
                df = df.sort_values(["symbol", "date"])
                latest = df.groupby("symbol").tail(1)
                for _, r in latest.iterrows():
                    today_close[str(r["symbol"])] = float(r["close"])
        except Exception as exc:
            logger.warning("close lookup in recompute failed: %s", exc)

    # 行业映射
    industry_name_map = ind_store.load_names() if ind_store else {}

    from akq_agents.services.portfolio.trade_list import generate_trade_list

    try:
        items = generate_trade_list(
            cohort_date=target_date,
            target_weights=weights,
            current_close=today_close,
            holdings=holdings_dict,
            composite_scores=composite,
            industry_map=industry_name_map,
            yesterday_weights=prev_weights,
            cfg=tl_cfg,
        )
        tl_store.upsert_cohort(target_date, items)
    except Exception as exc:
        logger.exception("recompute trade_list failed")
        return {"recomputed": False, "error": f"generate failed: {exc}"}
    return {"recomputed": True, "cohort_date": target_date.isoformat(), "n_items": len(items)}


@router.get("/holdings")
async def list_holdings() -> dict[str, Any]:
    """列出当前真实持仓。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        return {"holdings": [], "n": 0}
    rows = store.list_all()

    # 拼股票中文简称（UI 展示用）
    name_store = workflow.services.get("stock_name_store") if workflow else None
    if name_store is not None and rows:
        name_map = name_store.load_all()
        if name_map:
            for h in rows:
                h["name"] = name_map.get(str(h.get("symbol")), "")

    total_shares = sum(float(r["shares"]) for r in rows)
    return {"holdings": rows, "n": len(rows), "total_shares": total_shares}


@router.put("/holdings/{symbol}")
async def upsert_holding(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """手动校准持仓：shares / avg_cost / note。M24: 改完 fire-and-forget 写一个
    portfolio.trade_list_recompute trigger 到 daemon, 立即返回 200, 不阻塞 web.
    """
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        raise HTTPException(503, "holdings_store not ready")
    shares = float(payload.get("shares", 0))
    avg_cost = payload.get("avg_cost")
    if avg_cost is not None:
        avg_cost = float(avg_cost)
    note = payload.get("note")

    # P1-2: 新建持仓时必须给 avg_cost，否则下游 PnL/盈亏会 NaN
    if shares > 0:
        existing = store.get_shares(symbol)
        if existing <= 0 and avg_cost is None:
            raise HTTPException(
                400,
                detail="新建持仓必须提供 avg_cost（成本价）；如不知道可填当前价。"
            )
    store.upsert(symbol, shares, avg_cost=avg_cost, note=note)

    # M24: fire-and-forget trade_list 重算到 daemon. 失败也不影响 PUT 200.
    # 用 sched_store 直接写 trigger 避免走 trigger_job 的 409 检查 — 持仓 PUT 不该被
    # "已有 pending trade_list_recompute" 阻塞, 反正 picker 会按 FIFO 跑, 多个 trigger
    # 会让 trade_list 算多次 (取最后一个的结果).
    if svc.sched_store is not None:
        try:
            from akq_agents.web.api.control import _manual_partition
            partition = _manual_partition(_date.today().isoformat())
            trig_id = svc.sched_store.create_pending_trigger(
                job_id="portfolio.trade_list_recompute", partition=partition, payload={},
            )
            svc.sched_store.upsert_job_run(
                job_id="portfolio.trade_list_recompute", partition=partition, status="pending",
                payload={"trigger_id": trig_id, "from": "upsert_holding", "symbol": symbol},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_holding: enqueue trade_list_recompute failed (non-fatal): %s", exc)

    return {"status": "ok", "symbol": symbol, "shares": shares}


@router.delete("/holdings/{symbol}")
async def delete_holding(symbol: str) -> dict[str, Any]:
    """删除一只持仓（等价于 shares=0）。M24: 同上, fire-and-forget trade_list_recompute."""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("holdings_store") if workflow else None
    if store is None:
        raise HTTPException(503, "holdings_store not ready")
    store.upsert(symbol, 0.0)
    if svc.sched_store is not None:
        try:
            from akq_agents.web.api.control import _manual_partition
            partition = _manual_partition(_date.today().isoformat())
            trig_id = svc.sched_store.create_pending_trigger(
                job_id="portfolio.trade_list_recompute", partition=partition, payload={},
            )
            svc.sched_store.upsert_job_run(
                job_id="portfolio.trade_list_recompute", partition=partition, status="pending",
                payload={"trigger_id": trig_id, "from": "delete_holding", "symbol": symbol},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_holding: enqueue trade_list_recompute failed (non-fatal): %s", exc)
    return {"status": "ok"}


@router.post("/holdings/recompute")
async def recompute_trade_list_manual() -> dict[str, Any]:
    """M24: trade_list 重算走 daemon 异步通道. 立即 202 + result_poll_url.

    之前同步跑: 读 snapshot + 拉 close + generate_trade_list, 1-5s, 把 web event loop 阻塞.
    现在 web 立即 202, 前端用 result_poll_url 轮询 /jobs/portfolio.trade_list_recompute/{partition}/result.
    """
    from akq_agents.web.api.control import trigger_job as _trigger
    return await _trigger(name="portfolio.trade_list_recompute", body={})


@router.get("/today-list")
async def today_trade_list(date: str | None = None) -> dict[str, Any]:
    """获取某日（默认今日）的交易清单。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("trade_list_store") if workflow else None
    if store is None:
        return {"items": [], "n": 0}
    target_date = _date.fromisoformat(date) if date else _date.today()
    requested_date = target_date
    items = store.list_cohort(target_date)
    fallback_used = False

    if not items:
        # 找最近一天有清单的日期作为回退
        dates = store.list_dates(limit=1)
        if dates:
            target_date = _date.fromisoformat(dates[0])
            items = store.list_cohort(target_date)
            fallback_used = target_date != requested_date

    # 拼 stock name（代码 + 中文简称同时显示，避免用户只看到 6 位代码）
    name_store = workflow.services.get("stock_name_store") if workflow else None
    if name_store is not None and items:
        name_map = name_store.load_all()
        if name_map:
            for it in items:
                it["name"] = name_map.get(str(it.get("symbol")), "")

    # 汇总
    n_buy = sum(1 for it in items if it["action"] == "BUY")
    n_sell = sum(1 for it in items if it["action"] == "SELL")
    n_hold = sum(1 for it in items if it["action"] == "HOLD")
    total_buy_amt = sum(it["delta_amount"] for it in items if it["action"] == "BUY")
    total_sell_amt = sum(abs(it["delta_amount"]) for it in items if it["action"] == "SELL")

    # 修复 oracle #3：标注 staleness 让前端 / LLM 都能感知
    # M20: 区分 cohort_date (系统在哪一天算出这个组合) vs execution_date (用户该哪天下单).
    # daemon 在 t 16:30 算组合 (cohort_date=t), 但 t 已盘后无法下单 — 实际建议执行日是 t+1.
    # 所以 "今日清单" = cohort_date+1 ≤ today (用户今天还能照这清单下单).
    today_actual = _date.today()
    repo = workflow.services.get("data_repository") if workflow else None
    from datetime import timedelta as _td
    execution_date = target_date + _td(days=1)  # 兜底：自然日 +1
    if repo is not None and hasattr(repo, "_calendar"):
        try:
            execution_date = repo._calendar.next_trading_day(target_date)
        except Exception:
            # calendar 越界 (cohort 是已知日历最后一天) 时退回兜底，避免接口 500
            logger.warning("next_trading_day(%s) failed, fallback to +1 calendar day", target_date)
    # 用户视角的 "是不是今日清单":
    #   cohort=6/24 → execution=6/25, 用户今天 (6/25) 看 = 今日清单
    #   cohort=6/24 → execution=6/25, 用户后天 (6/26) 看 = 1 天前
    #   用户传 ?date=未来 → execution 也在未来, raw_delta<0, 标记为"未来清单"
    raw_delta = (today_actual - execution_date).days
    staleness_days = max(0, raw_delta)
    is_today = raw_delta == 0
    is_future = raw_delta < 0

    if is_today:
        stale_warning = None
    elif is_future:
        stale_warning = (
            f"⚠️ 当前清单建议执行日是 {execution_date.isoformat()}，尚未到达执行日。"
            f"请到当天再查看。"
        )
    else:
        stale_warning = (
            f"⚠️ 当前清单建议执行日是 {execution_date.isoformat()}，距今 {staleness_days} 天前。"
            f"今日盘后 16:30 会自动算出新组合（需为交易日）；"
            f"盘中（9:30–16:30）系统不重算组合，因子需收盘价才能定。"
        )

    return {
        "cohort_date": target_date.isoformat(),
        "requested_date": requested_date.isoformat(),
        "fallback_used": fallback_used,
        "execution_date": execution_date.isoformat(),
        "today": today_actual.isoformat(),
        "is_today": is_today,
        "staleness_days": staleness_days,
        "stale_warning": stale_warning,
        "n": len(items),
        "n_buy": n_buy,
        "n_sell": n_sell,
        "n_hold": n_hold,
        "total_buy_amount": total_buy_amt,
        "total_sell_amount": total_sell_amt,
        "items": items,
    }


@router.post("/today-list/{symbol}/mark-executed")
async def mark_executed(symbol: str, date: str | None = None) -> dict[str, Any]:
    """标记某条交易已执行 + 同步 holdings。"""
    svc = get_services()
    workflow = svc.workflow
    tl_store = workflow.services.get("trade_list_store") if workflow else None
    h_store = workflow.services.get("holdings_store") if workflow else None
    if tl_store is None:
        raise HTTPException(503, "trade_list_store not ready")
    target_date = _date.fromisoformat(date) if date else _date.today()
    tl_store.mark_executed(target_date, symbol, holdings_store=h_store)
    return {"status": "ok", "holdings_synced": h_store is not None}


@router.post("/today-list/mark-all-executed")
async def mark_all_executed(date: str | None = None) -> dict[str, Any]:
    """一键执行: 把指定日期 trade_list 里所有 BUY/SELL 全部 mark executed + 同步 holdings。"""
    svc = get_services()
    workflow = svc.workflow
    tl_store = workflow.services.get("trade_list_store") if workflow else None
    h_store = workflow.services.get("holdings_store") if workflow else None
    if tl_store is None:
        raise HTTPException(503, "trade_list_store not ready")
    target_date = _date.fromisoformat(date) if date else _date.today()
    items = tl_store.list_cohort(target_date)
    n_executed = 0
    n_failed = 0
    failed_symbols: list[str] = []
    for it in items:
        action = it.get("action")
        if action not in ("BUY", "SELL"):
            continue
        if it.get("executed"):
            continue
        try:
            tl_store.mark_executed(target_date, it["symbol"], holdings_store=h_store)
            n_executed += 1
        except Exception:
            logger.exception("mark_executed failed for %s", it.get("symbol"))
            n_failed += 1
            failed_symbols.append(it.get("symbol", "?"))
    return {
        "status": "ok" if n_failed == 0 else "partial",
        "executed_count": n_executed,
        "failed_count": n_failed,
        "failed_symbols": failed_symbols,
    }


@router.get("/dates")
async def list_trade_list_dates() -> dict[str, Any]:
    """所有有清单的日期（最近 30 天）。"""
    svc = get_services()
    workflow = svc.workflow
    store = workflow.services.get("trade_list_store") if workflow else None
    if store is None:
        return {"dates": []}
    return {"dates": store.list_dates(limit=30)}
