"""P4 LLM 可调工具实现：4 个只读工具（spec v2）。

- get_data_health        ← P1 DataRepository.quality_report
- list_factors           ← P3 FactorRegistry + FactorEvaluator
- get_portfolio_snapshot ← P3 PortfolioSnapshotStore
- query_events           ← P2 SchedulerStateStore

每个工具 build_* 函数接受装配好的 services 字典，返回 ToolSpec。
注册见 :func:`register_default_tools`。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from akq_agents.services.llm.tools.registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

# ---------------- 1. get_data_health ----------------


def build_get_data_health(services: dict[str, Any]) -> ToolSpec:
    repo = services["data_repository"]

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        health = repo.quality_report()
        return health.model_dump(mode="json")

    return ToolSpec(
        name="get_data_health",
        description="返回数据层当前健康状态：last_full_refresh / universe_size_today / ohlcv_coverage_today / pending_retries / unresolved_errors_24h / health=OK|DEGRADED|FAILED。无入参。",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


# ---------------- 2. list_factors ----------------


def build_list_factors(services: dict[str, Any]) -> ToolSpec:
    registry = services["factor_registry"]
    evaluator = services.get("factor_evaluator")  # 可选

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for f in registry.list_all():
            latest = None
            if evaluator is not None:
                m = evaluator.get_latest(f.name, f.factor_version)
                if m is not None:
                    latest = {
                        "as_of_date": m.as_of_date,
                        "ic_mean": m.ic_mean,
                        "ir": m.ir,
                        "status": m.status,
                    }
            rows.append(
                {
                    "name": f.name,
                    "factor_version": f.factor_version,
                    "direction": f.direction,
                    "lookback_days": f.lookback_days,
                    "last_metric": latest,
                }
            )
        return {"factors": rows, "n": len(rows)}

    return ToolSpec(
        name="list_factors",
        description="列出系统中所有已注册的因子及其最近一次评估指标（IC / IR / status）。无入参。",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


# ---------------- 3. get_portfolio_snapshot ----------------


def build_get_portfolio_snapshot(services: dict[str, Any]) -> ToolSpec:
    store = services["portfolio_snapshot_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        date_str = args["date"]
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            return {"error": "INVALID_ARGUMENTS", "detail": f"invalid date {date_str!r}, expected YYYY-MM-DD"}
        rows = store.read_snapshot(d)
        if not rows:
            return {"error": "NO_SNAPSHOT", "date": date_str}
        return {
            "as_of_date": date_str,
            "n": len(rows),
            "rows": [
                {
                    "symbol": r.symbol,
                    "name": r.name,
                    "industry": r.industry,
                    "weight": r.weight,
                    "prev_weight": r.prev_weight,
                    "composite_score": r.composite_score,
                    "top_factors": json.loads(r.top_factors_json or "[]"),
                }
                for r in rows
            ],
        }

    return ToolSpec(
        name="get_portfolio_snapshot",
        description="获取指定日期的组合快照：每只股票的 symbol/name/industry/weight/prev_weight/composite_score/top_factors。入参 date='YYYY-MM-DD'。",
        json_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "查询日期 YYYY-MM-DD"},
            },
            "required": ["date"],
        },
        handler=handler,
    )


# ---------------- 4. query_events ----------------


def build_query_events(services: dict[str, Any]) -> ToolSpec:
    store = services["scheduler_state_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind_prefix = args.get("kind_prefix")
        since = args.get("since")
        level_min = args.get("level_min")
        limit = int(args.get("limit", 50))
        if limit > 200:
            limit = 200

        # since 解析（支持 'YYYY-MM-DD' 或 ISO，或简写 '24h' / '7d'）
        since_iso: str | None = None
        if since:
            since_iso = _parse_since(since)

        events = store.list_events(
            limit=limit, level_min=level_min, kind_prefix=kind_prefix, since=since_iso
        )
        return {
            "events": [
                {
                    "ts": e.ts,
                    "level": e.level,
                    "kind": e.kind,
                    "source": e.source,
                    "payload": json.loads(e.payload_json) if e.payload_json else None,
                }
                for e in events
            ],
            "n": len(events),
        }

    return ToolSpec(
        name="query_events",
        description=(
            "查询调度器事件流。入参 kind_prefix (可选, 如 'batch.' / 'portfolio.'), since (可选, '24h'/'7d'/'YYYY-MM-DD' 或 ISO), level_min (info|warning|error), limit (默认 50, 上限 200)。"
        ),
        json_schema={
            "type": "object",
            "properties": {
                "kind_prefix": {"type": "string"},
                "since": {"type": "string"},
                "level_min": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
        handler=handler,
    )


def _parse_since(s: str) -> str:
    """支持 '24h' / '7d' / 'YYYY-MM-DD' / ISO 字符串。返回 ISO。"""
    s = s.strip()
    if s.endswith("h") and s[:-1].isdigit():
        delta = timedelta(hours=int(s[:-1]))
        return (datetime.now() - delta).isoformat()
    if s.endswith("d") and s[:-1].isdigit():
        delta = timedelta(days=int(s[:-1]))
        return (datetime.now() - delta).isoformat()
    # try date
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    # try iso datetime
    try:
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        # 退化：原样返回，让下游 SQL 比较去判定
        return s


# ---------------- registry helper ----------------


# ---------------- M5: 3 个新工具 ----------------


def build_get_factor_proposals(services: dict[str, Any]) -> ToolSpec:
    """列出自动发现引擎的因子候选流水（含 accepted/rejected + 决策原因）。"""
    store = services["factor_proposal_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        status = args.get("status") or None
        if status and status not in {"accepted", "rejected", "pending"}:
            return {"error": "INVALID_ARGUMENTS", "detail": "status must be accepted/rejected/pending"}
        rows = store.list_recent(limit=limit, status=status)
        return {
            "counts": store.counts(),
            "n": len(rows),
            "rows": [
                {
                    "factor_name": r.factor_name,
                    "status": r.status,
                    "ir": r.ir,
                    "ic_mean": r.ic_mean,
                    "max_abs_corr": r.max_abs_corr,
                    "reason": r.reason,
                    "recipe": json.loads(r.recipe_json),
                    "evaluated_at": r.evaluated_at,
                }
                for r in rows
            ],
        }

    return ToolSpec(
        name="get_factor_proposals",
        description="查看自动因子发现的候选流水（每条记录含 recipe / IR / IC / 决策原因）。可按 status 过滤。",
        json_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "status": {"type": "string", "enum": ["accepted", "rejected", "pending"]},
            },
            "required": [],
        },
        handler=handler,
    )


def build_run_factor_discovery(services: dict[str, Any]) -> ToolSpec:
    """同步触发一轮因子自动发现并返回统计。"""
    engine = services["discovery_engine"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        n = int(args.get("n_candidates", 10))
        if n < 1 or n > 50:
            return {"error": "INVALID_ARGUMENTS", "detail": "n_candidates must be in [1, 50]"}
        stats = engine.run_batch(n_candidates=n)
        return stats.as_dict()

    return ToolSpec(
        name="run_factor_discovery",
        description="手动触发一轮自动因子发现（从 DSL 空间随机抽样 n 个候选 → 评估 IC/IR → 通过门槛者注册）。慢操作，1-2 分钟。",
        json_schema={
            "type": "object",
            "properties": {
                "n_candidates": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": [],
        },
        handler=handler,
    )


def build_get_nav_summary(services: dict[str, Any]) -> ToolSpec:
    """查询 M7-A 组合净值回测结果（扣费 NAV、夏普、回撤、超额）。"""
    backtester = services["portfolio_backtester"]

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        df = backtester.read_nav()
        if df.empty:
            return {"error": "NO_NAV_DATA", "detail": "尚未生成 NAV，运行回填脚本或等待 daemon 跑完一次盘后批处理"}
        summary = backtester._summarize(df)
        # 加几个最近值方便对话
        last = df.iloc[-1]
        summary["latest_date"] = str(last["as_of_date"])
        summary["latest_nav_net"] = float(last["nav_net"])
        summary["latest_benchmark_nav"] = float(last["benchmark_nav"]) if last["benchmark_nav"] is not None else None
        return summary

    return ToolSpec(
        name="get_nav_summary",
        description="查询组合扣费净值回测结果：累计收益、年化、夏普、最大回撤、超额收益（vs 沪深300）、平均换手率、总成本。无入参。",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def build_explain_portfolio(services: dict[str, Any]) -> ToolSpec:
    """对某日组合做自然语言友好的归因摘要。"""
    store = services["portfolio_snapshot_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        date_str = args.get("date") or date.today().isoformat()
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            return {"error": "INVALID_ARGUMENTS", "detail": f"invalid date {date_str!r}"}
        rows = store.read_snapshot(d)
        if not rows:
            return {"error": "NO_SNAPSHOT", "date": date_str}
        # top-10 重权 + 因子贡献加总
        top = sorted(rows, key=lambda r: r.weight, reverse=True)[:10]
        factor_total: dict[str, float] = {}
        for r in rows:
            top_factors_raw = getattr(r, "top_factors_json", None) or "[]"
            try:
                tf = json.loads(top_factors_raw)
            except (json.JSONDecodeError, TypeError):
                tf = []
            for f in tf:
                factor_total[f["name"]] = factor_total.get(f["name"], 0.0) + float(f.get("contribution", 0.0))
        factor_total_sorted = sorted(factor_total.items(), key=lambda x: -abs(x[1]))[:5]
        return {
            "as_of_date": date_str,
            "n_holdings": len(rows),
            "top_holdings": [
                {"symbol": r.symbol, "weight": r.weight, "composite_score": r.composite_score}
                for r in top
            ],
            "dominant_factors": [
                {"factor_name": name, "total_contribution": v} for name, v in factor_total_sorted
            ],
        }

    return ToolSpec(
        name="explain_portfolio",
        description="对某日组合做摘要：top-10 持仓 + 5 个主导因子（按贡献绝对值排序）。可不传 date，默认今日。",
        json_schema={
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": [],
        },
        handler=handler,
    )


# ============================================================
# P1-3: 对比类工具（让 LLM 能做"比较与因果"而不是只"打开抽屉"）
# ============================================================


def build_diff_portfolio(services: dict[str, Any]) -> ToolSpec:
    """对比两天的组合：谁进了 / 谁出了 / 谁的权重变化最大。"""
    store = services["portfolio_snapshot_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        date_a = args.get("date_a")
        date_b = args.get("date_b")
        if not date_a or not date_b:
            return {"error": "INVALID_ARGUMENTS", "detail": "需要 date_a 和 date_b"}
        try:
            da = date.fromisoformat(date_a)
            db = date.fromisoformat(date_b)
        except ValueError:
            return {"error": "INVALID_ARGUMENTS", "detail": "日期格式 YYYY-MM-DD"}

        rows_a = {r.symbol: r for r in store.read_snapshot(da)}
        rows_b = {r.symbol: r for r in store.read_snapshot(db)}
        if not rows_a:
            return {"error": "NO_SNAPSHOT", "date": date_a}
        if not rows_b:
            return {"error": "NO_SNAPSHOT", "date": date_b}

        # 进入 (在 b 不在 a)
        entered = []
        for sym, r in rows_b.items():
            if sym not in rows_a:
                entered.append({
                    "symbol": sym, "weight_b": r.weight,
                    "composite_score": r.composite_score,
                    "industry": r.industry,
                })
        # 退出 (在 a 不在 b)
        exited = []
        for sym, r in rows_a.items():
            if sym not in rows_b:
                exited.append({
                    "symbol": sym, "weight_a": r.weight,
                    "industry": r.industry,
                })
        # 权重变化（两天都在）
        changed = []
        for sym, ra in rows_a.items():
            if sym in rows_b:
                rb = rows_b[sym]
                delta = rb.weight - ra.weight
                changed.append({
                    "symbol": sym,
                    "weight_a": ra.weight,
                    "weight_b": rb.weight,
                    "delta": delta,
                    "industry": rb.industry,
                })
        # 按 |delta| 排序
        changed.sort(key=lambda x: -abs(x["delta"]))
        # turnover = 0.5 * Σ|delta|
        all_syms = set(rows_a) | set(rows_b)
        turnover = 0.5 * sum(
            abs(rows_b.get(s, type("X", (), {"weight": 0})).weight - rows_a.get(s, type("X", (), {"weight": 0})).weight)
            for s in all_syms
        )

        return {
            "date_a": date_a, "date_b": date_b,
            "n_holdings_a": len(rows_a), "n_holdings_b": len(rows_b),
            "n_entered": len(entered),
            "n_exited": len(exited),
            "turnover": turnover,
            "entered_top": sorted(entered, key=lambda x: -x["weight_b"])[:10],
            "exited_top": sorted(exited, key=lambda x: -x["weight_a"])[:10],
            "weight_changes_top": changed[:15],
        }

    return ToolSpec(
        name="diff_portfolio",
        description="对比两天的组合差异：谁新进、谁退出、谁的权重变化最大、总换手率。用于「为什么今天换手比上周高一倍」这种问题。",
        json_schema={
            "type": "object",
            "properties": {
                "date_a": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "date_b": {"type": "string", "description": "对比日期 YYYY-MM-DD"},
            },
            "required": ["date_a", "date_b"],
        },
        handler=handler,
    )


def build_factor_decay_check(services: dict[str, Any]) -> ToolSpec:
    """某因子最近 N 天 IC 趋势：是否在衰减？"""
    evaluator = services["factor_evaluator"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("factor_name")
        if not name:
            return {"error": "INVALID_ARGUMENTS", "detail": "需要 factor_name"}
        lookback = int(args.get("lookback_days", 60))

        history = evaluator.list_history(name, limit=200)
        if not history:
            return {"error": "NO_HISTORY", "factor_name": name}

        # history 倒序，截最近 N 个
        recent = history[:lookback]
        if len(recent) < 3:
            return {
                "factor_name": name,
                "n_observations": len(recent),
                "verdict": "数据不足",
                "history": [{"date": m.as_of_date, "ic": m.ic_mean, "ir": m.ir, "status": m.status} for m in recent],
            }

        irs = [float(m.ir) for m in recent if m.ir is not None]
        ics = [float(m.ic_mean) for m in recent if m.ic_mean is not None]
        if not irs:
            return {"error": "NO_IR", "factor_name": name}

        # 前半段 vs 后半段：是否衰减
        mid = len(irs) // 2
        ir_recent = sum(irs[:mid]) / max(mid, 1)        # 最近一半（更近）
        ir_earlier = sum(irs[mid:]) / max(len(irs) - mid, 1)  # 较早一半
        ir_peak = max(abs(ir) for ir in irs)
        ir_latest = irs[0] if irs else 0

        verdict = "稳定"
        if abs(ir_recent) < 0.6 * abs(ir_earlier) and abs(ir_earlier) > 0.1:
            verdict = "⚠️ 显著衰减"
        elif abs(ir_recent) < 0.8 * abs(ir_earlier) and abs(ir_earlier) > 0.1:
            verdict = "轻微衰减"
        elif abs(ir_recent) > 1.2 * abs(ir_earlier):
            verdict = "改善"

        # 列史
        sample = [
            {"date": m.as_of_date, "ic": round(float(m.ic_mean), 4) if m.ic_mean is not None else None,
             "ir": round(float(m.ir), 4) if m.ir is not None else None, "status": m.status}
            for m in recent[:30]
        ]

        return {
            "factor_name": name,
            "verdict": verdict,
            "n_observations": len(recent),
            "ir_latest": round(ir_latest, 4),
            "ir_recent_half_avg": round(ir_recent, 4),
            "ir_earlier_half_avg": round(ir_earlier, 4),
            "ir_peak_abs": round(ir_peak, 4),
            "history_sample": sample,
        }

    return ToolSpec(
        name="factor_decay_check",
        description="检查某因子最近 N 天 IC/IR 趋势：前半段 vs 后半段，给出「稳定 / 轻微衰减 / 显著衰减 / 改善」判定。用于「这个因子是不是快不行了」。",
        json_schema={
            "type": "object",
            "properties": {
                "factor_name": {"type": "string"},
                "lookback_days": {"type": "integer", "default": 60},
            },
            "required": ["factor_name"],
        },
        handler=handler,
    )


def build_attribute_nav_drop(services: dict[str, Any]) -> ToolSpec:
    """期间 NAV 跌幅按个股 P&L 真贡献拆解（不是评分贡献）。"""
    backtester = services["portfolio_backtester"]
    snapshot_store = services["portfolio_snapshot_store"]

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        date_start = args.get("date_start")
        date_end = args.get("date_end")
        if not date_start or not date_end:
            return {"error": "INVALID_ARGUMENTS", "detail": "需要 date_start 和 date_end"}

        df = backtester.read_nav()
        if df.empty:
            return {"error": "NO_NAV_DATA"}

        df_sub = df[(df["as_of_date"] >= date_start) & (df["as_of_date"] <= date_end)]
        if df_sub.empty:
            return {"error": "NO_DATA_IN_RANGE", "date_start": date_start, "date_end": date_end}

        n = len(df_sub)
        nav_start = float(df_sub.iloc[0]["nav_net"])
        nav_end = float(df_sub.iloc[-1]["nav_net"])
        total_return = (nav_end / nav_start) - 1.0 if nav_start > 0 else 0.0
        max_dd_in_range = float(((df_sub["nav_net"] / df_sub["nav_net"].cummax()) - 1.0).min())
        worst_day_idx = df_sub["daily_return_net"].idxmin()
        worst_day = df_sub.loc[worst_day_idx]
        worst_date_str = worst_day["as_of_date"]

        # 修复 oracle #5：真实 P&L 拆解
        # 找 worst_date 的前一日 snapshot（昨日权重） + worst_date / 前一日 ohlcv close
        # 个股 bps 贡献 = (close_t / close_{t-1} - 1) × prev_weight × 10000
        contributions: list[dict] = []
        try:
            worst_d = date.fromisoformat(worst_date_str)
            # 前一日 snapshot：从 NAV df 找上一行
            pos = df_sub.index.get_loc(worst_day_idx)
            if pos > 0:
                prev_date_str = df_sub.iloc[pos - 1]["as_of_date"]
                prev_date_d = date.fromisoformat(prev_date_str)
                prev_holdings = snapshot_store.read_snapshot(prev_date_d)
                if prev_holdings:
                    symbols = [r.symbol for r in prev_holdings]
                    # 查 worst_date / prev_date 两天的 close
                    from akq_agents.web.deps import get_services as _gs
                    svc2 = _gs()
                    repo = svc2.repo
                    if repo is not None:
                        import pyarrow.dataset as ds
                        dataset = ds.dataset(repo._ohlcv_dir, format="parquet", partitioning="hive")
                        table = dataset.to_table(
                            filter=(ds.field("date").isin([prev_date_d.isoformat(), worst_d.isoformat()]))
                                   & ds.field("symbol").isin(symbols),
                            columns=["date", "symbol", "close"],
                        )
                        ohlcv = table.to_pandas()
                        if not ohlcv.empty:
                            # close pivot
                            close = ohlcv.pivot_table(index="symbol", columns="date", values="close", aggfunc="last")
                            # 列名是 date object 或 str；统一处理
                            cols = list(close.columns)
                            prev_col = next((c for c in cols if str(c) == prev_date_d.isoformat()), None)
                            worst_col = next((c for c in cols if str(c) == worst_d.isoformat()), None)
                            if prev_col is not None and worst_col is not None:
                                for r in prev_holdings:
                                    sym = r.symbol
                                    if sym in close.index:
                                        p0 = close.at[sym, prev_col]
                                        p1 = close.at[sym, worst_col]
                                        if p0 is not None and p1 is not None and not pd.isna(p0) and not pd.isna(p1) and p0 > 0:
                                            stock_ret = (float(p1) / float(p0)) - 1.0
                                            contribution_bps = stock_ret * float(r.weight) * 10000
                                            contributions.append({
                                                "symbol": sym,
                                                "industry": r.industry,
                                                "prev_weight": float(r.weight),
                                                "prev_price": float(p0),
                                                "today_price": float(p1),
                                                "stock_return_pct": stock_ret,
                                                "contribution_bps": contribution_bps,
                                            })
        except Exception as exc:
            logger.warning("attribute_nav_drop P&L decomposition failed: %s", exc)

        contributions.sort(key=lambda x: x["contribution_bps"])
        top_drags = contributions[:5]              # 拖累最大（最负）
        top_boosts = list(reversed(contributions[-5:]))  # 拉抬最大（最正）

        # benchmark
        bench_start = df_sub.iloc[0]["benchmark_nav"]
        bench_end = df_sub.iloc[-1]["benchmark_nav"]
        bench_return = (bench_end / bench_start - 1.0) if (bench_start and bench_start > 0) else None

        return {
            "date_start": date_start, "date_end": date_end,
            "n_days": n,
            "nav_start": nav_start, "nav_end": nav_end,
            "total_return_net": total_return,
            "max_drawdown_in_range": max_dd_in_range,
            "worst_day": {
                "date": worst_date_str,
                "daily_return": float(worst_day["daily_return_net"]) if worst_day["daily_return_net"] is not None else None,
                "turnover": float(worst_day["turnover"]) if worst_day["turnover"] is not None else None,
            },
            "worst_day_top_drags": top_drags,
            "worst_day_top_boosts": top_boosts,
            "decomposition_note": (
                "contribution_bps = (close_t / close_{t-1} - 1) × prev_weight × 10000，"
                "代表该股票当日真实 P&L 对组合的 bps 贡献（不是因子评分贡献）。"
                "Top drags = 拖累最大（最负），top boosts = 拉抬最大（最正）。"
            ),
            "benchmark_return": bench_return,
            "excess_return": (total_return - bench_return) if bench_return is not None else None,
        }

    return ToolSpec(
        name="attribute_nav_drop",
        description="对某段时间的 NAV 表现做**真实 P&L 归因**：找最差一天 → 拆解到每只持仓的 contribution_bps（按 prev_weight × 当日股票收益）→ top 5 拖累 + top 5 拉抬。用于「最近 30 天为什么跑输沪深300」「上周哪天跌得最惨，哪些票拖累」。",
        json_schema={
            "type": "object",
            "properties": {
                "date_start": {"type": "string", "description": "YYYY-MM-DD"},
                "date_end": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["date_start", "date_end"],
        },
        handler=handler,
    )


# ============================================================
# L-4: 暴露 trade_list 和 paper_trading 让 chat 能问
# ============================================================


def build_get_today_trade_list(services: dict[str, Any]) -> ToolSpec:
    """返回当前 holdings 推算出的今日 BUY/SELL/HOLD 清单。"""
    tl_store = services["trade_list_store"]

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        dates = tl_store.list_dates(limit=1)
        if not dates:
            return {"error": "NO_TRADE_LIST"}
        from datetime import date as _date

        target = _date.fromisoformat(dates[0])
        today_actual = _date.today()
        staleness_days = (today_actual - target).days
        items = tl_store.list_cohort(target)
        n_buy = sum(1 for it in items if it["action"] == "BUY")
        n_sell = sum(1 for it in items if it["action"] == "SELL")
        n_hold = sum(1 for it in items if it["action"] == "HOLD")
        total_buy = sum(it["delta_amount"] for it in items if it["action"] == "BUY")
        total_sell = sum(abs(it["delta_amount"]) for it in items if it["action"] == "SELL")
        tradable = [
            {k: v for k, v in it.items() if k != "executed"}
            for it in items if it["action"] != "HOLD"
        ][:30]
        out = {
            "cohort_date": target.isoformat(),
            "today": today_actual.isoformat(),
            "is_today": staleness_days == 0,
            "staleness_days": staleness_days,
            "n_buy": n_buy, "n_sell": n_sell, "n_hold": n_hold,
            "total_buy_amount": total_buy,
            "total_sell_amount": total_sell,
            "tradable": tradable,
        }
        if staleness_days > 0:
            out["stale_warning"] = (
                f"⚠️ 注意：此清单生成于 {staleness_days} 天前（cohort_date={target.isoformat()}），"
                f"今天是 {today_actual.isoformat()}。回答用户时必须明确告知"
                f"「当前清单非今日，仅供参考，今日盘后将自动刷新」，不要让用户误以为是今日实时建议。"
            )
        return out

    return ToolSpec(
        name="get_today_trade_list",
        description="返回根据当前真实持仓推算的今日交易清单：BUY/SELL/HOLD + 具体股数 + 金额 + 中文原因。注意：如果返回中含 stale_warning 字段，必须在回答里照实告知用户。",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def build_get_paper_track_summary(services: dict[str, Any]) -> ToolSpec:
    """前向跟踪：30/60/90 天后系统推荐组合的真实表现（含超额 vs 沪深300）。"""
    paper = services["paper_trading_store"]

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return paper.summary()

    return ToolSpec(
        name="get_paper_track_summary",
        description="返回前向跟踪 paper trading 的汇总：所有 cohort 在 30/60/90 天后的平均收益 + 胜率 + 超额 vs 沪深300。用于「过去推荐的组合 90 天后真的赚了多少？」",
        json_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )



def build_factor_postmortem(services: dict[str, Any]) -> ToolSpec:
    evaluator = services["factor_evaluator"]
    proposal_store = services.get("factor_proposal_store")
    registry_obj = services.get("factor_registry")

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        factor_name = str(args.get("factor_name", "")).strip()
        days = int(args.get("days", 30))
        if not factor_name:
            return {"error": "factor_name required"}

        try:
            history = evaluator.list_history(factor_name, limit=days)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"list_history failed: {str(exc)[:200]}"}

        history_dicts = [
            {
                "as_of": m.as_of_date,
                "ic": m.ic_mean,
                "ir": m.ir,
                "window_days": m.window_days,
            }
            for m in history
        ]

        status = "unknown"
        if proposal_store is not None:
            try:
                for proposal in proposal_store.list_recent(limit=500):
                    if proposal.factor_name == factor_name:
                        status = proposal.status
                        break
            except Exception:  # noqa: BLE001
                pass
        if status == "unknown" and registry_obj is not None:
            try:
                for factor in registry_obj.list_all():
                    if factor.name == factor_name:
                        status = "registered"
                        break
            except Exception:  # noqa: BLE001
                pass

        irs = [item["ir"] for item in history_dicts if item["ir"] is not None]
        recent_mean = None
        earlier_mean = None
        trend = None
        if len(irs) >= 5:
            recent_mean = sum(abs(x) for x in irs[:5]) / 5
        if len(irs) >= 10:
            earlier_mean = sum(abs(x) for x in irs[-5:]) / 5
        if recent_mean is not None and earlier_mean is not None and earlier_mean > 0:
            ratio = recent_mean / earlier_mean
            if ratio < 0.6:
                trend = "decaying"
            elif ratio > 1.4:
                trend = "improving"
            else:
                trend = "stable"

        return {
            "factor_name": factor_name,
            "status": status,
            "history": history_dicts,
            "n_observations": len(history_dicts),
            "recent_5d_mean_abs_ir": recent_mean,
            "earlier_5d_mean_abs_ir": earlier_mean,
            "trend": trend,
        }

    return ToolSpec(
        name="factor_postmortem",
        description=(
            "查询某个因子的近 N 天 IC/IR 历史 + 当前 status + 趋势诊断。"
            "用途: 帮你判断一个 shadow 因子该 promote / demote / 继续观察。\n"
            "入参:\n"
            "- factor_name: 因子名 (如 'momentum_20' 或 'llm_zscore_close_30_long_abc123')\n"
            "- days: 看多少天历史, 默认 30\n"
            "返回:\n"
            "- status: registered/shadow/accepted/rejected/demoted/llm_suggested/unknown\n"
            "- history: 按 as_of 降序的 [{as_of, ic, ir, window_days}]\n"
            "- recent_5d_mean_abs_ir / earlier_5d_mean_abs_ir: 用于趋势对比\n"
            "- trend: decaying / stable / improving / None (数据不足)"
        ),
        json_schema={
            "type": "object",
            "properties": {
                "factor_name": {
                    "type": "string",
                    "description": "因子名 (如 momentum_20)",
                },
                "days": {
                    "type": "integer",
                    "description": "看多少天历史, 默认 30",
                    "default": 30,
                },
            },
            "required": ["factor_name"],
        },
        handler=handler,
    )


def register_default_tools(registry: ToolRegistry, services: dict[str, Any]) -> ToolRegistry:
    """注册 P4 v2 的 4 个默认工具 + M5 的 3 个新工具。

    依赖 services 中：
    - data_repository (P1)
    - factor_registry (P3), factor_evaluator (P3, 可选)
    - portfolio_snapshot_store (P3)
    - scheduler_state_store (P2)
    - factor_proposal_store (M2), discovery_engine (M2) — 可选

    缺任一关键 service → 跳过对应工具（不抛异常）。
    """
    if "data_repository" in services:
        registry.register(build_get_data_health(services))
    if "factor_registry" in services:
        registry.register(build_list_factors(services))
    if "portfolio_snapshot_store" in services:
        registry.register(build_get_portfolio_snapshot(services))
        registry.register(build_explain_portfolio(services))
        registry.register(build_diff_portfolio(services))    # P1-3
    if "portfolio_backtester" in services:
        registry.register(build_get_nav_summary(services))
        if "portfolio_snapshot_store" in services:
            registry.register(build_attribute_nav_drop(services))  # P1-3
    if "scheduler_state_store" in services:
        registry.register(build_query_events(services))
    if "factor_proposal_store" in services:
        registry.register(build_get_factor_proposals(services))
    if "discovery_engine" in services:
        registry.register(build_run_factor_discovery(services))
    if "factor_evaluator" in services:
        registry.register(build_factor_decay_check(services))   # P1-3
        registry.register(build_factor_postmortem(services))
    if "trade_list_store" in services:
        registry.register(build_get_today_trade_list(services))  # L-4
    if "paper_trading_store" in services:
        registry.register(build_get_paper_track_summary(services))  # L-4
    return registry
