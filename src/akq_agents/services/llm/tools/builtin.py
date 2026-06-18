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
from datetime import date, datetime, timedelta
from typing import Any

from akq_agents.services.llm.tools.registry import ToolRegistry, ToolSpec

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
    if "scheduler_state_store" in services:
        registry.register(build_query_events(services))
    if "factor_proposal_store" in services:
        registry.register(build_get_factor_proposals(services))
    if "discovery_engine" in services:
        registry.register(build_run_factor_discovery(services))
    return registry
