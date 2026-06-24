from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pprint import pprint

from akq_agents.bootstrap import (
    BASE_DIR,
    build_daemon,
    build_data_repository,
    build_workflow,
    load_data_config,
)
from akq_agents.models.data_config import DataConfig
from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
from akq_agents.orchestrator.state_store import SchedulerStateStore
from akq_agents.services.data.exceptions import DataNotReady
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.environment import EnvironmentDoctor


def cmd_run(_: argparse.Namespace) -> None:
    workflow, _cfg = build_workflow()
    workflow.run_once_and_print()


def cmd_doctor(_: argparse.Namespace) -> None:
    doctor = EnvironmentDoctor()
    pprint(doctor.check())


def _require_data_config() -> DataConfig:
    data_config = load_data_config()
    if data_config is None:
        print("data config not found: config/data.yaml", file=sys.stderr)
        sys.exit(1)
    return data_config


def _build_data_repo_with_calendar() -> DataRepository:
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    repo._calendar.bootstrap(lambda: repo._gateway.fetch_trading_dates())
    return repo


def cmd_data_bootstrap(args: argparse.Namespace) -> None:
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    repo._calendar.bootstrap(lambda: repo._gateway.fetch_trading_dates())
    lookback_days = args.lookback
    if lookback_days is None:
        lookback_days = data_config.cache.ohlcv_lookback_days
    repo.bootstrap_history(
        lookback_days=lookback_days,
        progress_cb=lambda done, total, status: print(
            f"[{done}/{total}] {status}" if status != "ok" else f"[{done}/{total}] done"
        ),
    )
    print(f"bootstrap complete: lookback_days={lookback_days}")
    print(repo.quality_report().model_dump_json(indent=2))


def cmd_data_refresh(args: argparse.Namespace) -> None:
    repo = _build_data_repo_with_calendar()
    target_date = date.today() if args.date is None else date.fromisoformat(args.date)
    result = repo.refresh_daily(target_date)
    print(result.model_dump_json(indent=2))


def cmd_data_status(_: argparse.Namespace) -> None:
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    print(repo.quality_report().model_dump_json(indent=2))


def cmd_data_inspect(args: argparse.Namespace) -> None:
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    end = date.today()
    start = end - timedelta(days=30)
    try:
        df = repo.get_ohlcv([args.symbol], start, end)
    except DataNotReady as exc:
        missing_days = ", ".join(day.isoformat() for day in exc.missing.get(args.symbol, []))
        print(f"no cached data for {args.symbol}; missing days: {missing_days}")
        return
    print(df.to_string())


# ---------------- P2 daemon commands ----------------


def _daemon_paths() -> tuple[DaemonStateFile, SchedulerStateStore]:
    """共用的 DaemonStateFile / StateStore 路径计算。"""
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    base_dir = repo._base_dir
    return DaemonStateFile(base_dir / "daemon_state.json"), SchedulerStateStore(base_dir / "meta.db")


def cmd_daemon_start(_: argparse.Namespace) -> None:
    """前台启动 daemon；Ctrl+C 触发优雅停机。"""
    daemon = build_daemon(install_signals=True)
    print("daemon starting (Ctrl+C to stop) ...")
    daemon.start(block=True)


def cmd_daemon_status(_: argparse.Namespace) -> None:
    """读 daemon_state.json + 心跳判定，输出 JSON。"""
    state_file, _store = _daemon_paths()
    state = state_file.read()
    # heartbeat 周期 5min × 2 = 10min 作为活跃判定阈值
    is_alive = state_file.is_alive(max_age_s=600)
    payload = {
        "state": state.to_dict() if state else None,
        "is_alive": is_alive,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_daemon_runs(args: argparse.Namespace) -> None:
    """列出最近的 job_runs。"""
    _state_file, store = _daemon_paths()
    runs = store.list_recent_runs(limit=args.last, job_id=args.job_id, status=args.status)
    rows = [
        {
            "id": r.id,
            "job_id": r.job_id,
            "partition": r.partition,
            "status": r.status,
            "reason_code": r.reason_code,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_ms": r.duration_ms,
        }
        for r in runs
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_daemon_events(args: argparse.Namespace) -> None:
    """列出最近的 events。"""
    _state_file, store = _daemon_paths()
    events = store.list_events(limit=args.last, level_min=args.level, kind_prefix=args.kind_prefix)
    rows = [
        {
            "id": e.id,
            "ts": e.ts,
            "level": e.level,
            "kind": e.kind,
            "source": e.source,
            "payload": json.loads(e.payload_json) if e.payload_json else None,
        }
        for e in events
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


# ---------------- P3 factors / portfolio commands ----------------


def cmd_factors_list(_: argparse.Namespace) -> None:
    """列出所有因子及最近 metrics。"""
    from akq_agents.services.factors import build_default_registry
    from akq_agents.services.portfolio import FactorEvaluator

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    evaluator = FactorEvaluator(meta_db_path=repo._base_dir / "meta.db", window=60)
    reg = build_default_registry()
    rows = []
    for f in reg.list_all():
        latest = evaluator.get_latest(f.name, f.factor_version)
        rows.append(
            {
                "name": f.name,
                "version": f.factor_version,
                "direction": f.direction,
                "lookback_days": f.lookback_days,
                "last_ic": latest.ic_mean if latest else None,
                "last_ir": latest.ir if latest else None,
                "last_evaluated": latest.as_of_date if latest else None,
                "status": latest.status if latest else None,
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_factors_inspect(args: argparse.Namespace) -> None:
    """打印某因子的历史 metrics。"""
    from akq_agents.services.portfolio import FactorEvaluator

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    evaluator = FactorEvaluator(meta_db_path=repo._base_dir / "meta.db", window=60)
    metrics = evaluator.list_history(args.name, limit=args.limit)
    rows = [
        {
            "factor_version": m.factor_version,
            "as_of_date": m.as_of_date,
            "window_days": m.window_days,
            "ic_mean": m.ic_mean,
            "ic_std": m.ic_std,
            "ir": m.ir,
            "t_stat": m.t_stat,
            "status": m.status,
            "reason": m.reason,
        }
        for m in metrics
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_factors_discover(args: argparse.Namespace) -> None:
    """手动跑一轮因子自动发现。"""
    from akq_agents.bootstrap import build_workflow

    workflow, _ = build_workflow()
    services = workflow.services
    engine = services.get("discovery_engine")
    if engine is None:
        print(json.dumps({"error": "discovery_engine not available; data layer required"}))
        return
    stats = engine.run_batch(n_candidates=args.n)
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))


def cmd_factors_proposals(args: argparse.Namespace) -> None:
    """列出因子提案流水。"""
    from akq_agents.bootstrap import build_workflow

    workflow, _ = build_workflow()
    store = workflow.services.get("factor_proposal_store")
    if store is None:
        print(json.dumps({"error": "factor_proposal_store not available"}))
        return
    rows = store.list_recent(limit=args.limit, status=args.status)
    out = [
        {
            "factor_name": r.factor_name,
            "status": r.status,
            "ir": r.ir,
            "ic_mean": r.ic_mean,
            "t_stat": r.t_stat,
            "max_abs_corr": r.max_abs_corr,
            "reason": r.reason,
            "recipe": json.loads(r.recipe_json),
            "evaluated_at": r.evaluated_at,
        }
        for r in rows
    ]
    print(json.dumps({"counts": store.counts(), "rows": out}, ensure_ascii=False, indent=2))


def cmd_portfolio_explain(args: argparse.Namespace) -> None:
    """打印某日组合快照。"""
    from datetime import date as _date

    from akq_agents.services.portfolio import PortfolioSnapshotStore

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    store = PortfolioSnapshotStore(meta_db_path=repo._base_dir / "meta.db")
    d = _date.today() if args.date is None else _date.fromisoformat(args.date)
    rows = store.read_snapshot(d)
    if not rows:
        print(json.dumps({"error": "no_snapshot_for_date", "date": d.isoformat()}, ensure_ascii=False))
        return
    payload = {
        "as_of_date": d.isoformat(),
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
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_trade_list(args: argparse.Namespace) -> None:
    """打印某日 BUY/SELL/HOLD 交易清单。"""
    from datetime import date as _date

    from akq_agents.services.portfolio.trade_list import TradeListStore

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    store = TradeListStore(meta_db_path=repo._base_dir / "meta.db")
    d = _date.today() if args.date is None else _date.fromisoformat(args.date)
    items = store.list_cohort(d)
    if not items:
        dates = store.list_dates(limit=1)
        if dates:
            d = _date.fromisoformat(dates[0])
            items = store.list_cohort(d)
    n_buy = sum(1 for it in items if it["action"] == "BUY")
    n_sell = sum(1 for it in items if it["action"] == "SELL")
    n_hold = sum(1 for it in items if it["action"] == "HOLD")
    total_buy = sum(it["delta_amount"] for it in items if it["action"] == "BUY")
    total_sell = sum(abs(it["delta_amount"]) for it in items if it["action"] == "SELL")
    payload = {
        "cohort_date": d.isoformat(),
        "n_buy": n_buy, "n_sell": n_sell, "n_hold": n_hold,
        "total_buy_amount": round(total_buy, 2),
        "total_sell_amount": round(total_sell, 2),
        "tradable": [it for it in items if it["action"] != "HOLD"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_paper_summary(_: argparse.Namespace) -> None:
    """打印 Paper Trading 前向跟踪汇总。"""
    from akq_agents.services.portfolio.paper_trading import PaperTradingStore, PaperTradingConfig

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    store = PaperTradingStore(repo._base_dir / "meta.db", PaperTradingConfig())
    print(json.dumps(store.summary(), ensure_ascii=False, indent=2))


def cmd_holdings_list(_: argparse.Namespace) -> None:
    """打印当前真实持仓。"""
    from akq_agents.services.portfolio.trade_list import HoldingsStore

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    store = HoldingsStore(meta_db_path=repo._base_dir / "meta.db")
    print(json.dumps({"holdings": store.list_all()}, ensure_ascii=False, indent=2))


def cmd_holdings_set(args: argparse.Namespace) -> None:
    """设置某只票的真实持仓（CLI 校准入口）。"""
    from akq_agents.services.portfolio.trade_list import HoldingsStore

    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    store = HoldingsStore(meta_db_path=repo._base_dir / "meta.db")
    store.upsert(args.symbol, float(args.shares),
                 avg_cost=float(args.cost) if args.cost else None,
                 note=args.note)
    print(json.dumps({"status": "ok", "symbol": args.symbol, "shares": float(args.shares)}, ensure_ascii=False))


# ---------------- P4 chat / llm commands ----------------


def cmd_chat(_: argparse.Namespace) -> None:
    """启动 ChatAgent REPL。"""
    from akq_agents.agents.chat_agent import ChatAgent
    from akq_agents.bootstrap import build_workflow

    workflow, _cfg = build_workflow()
    workflow_services = workflow.services
    if "llm_orchestrator" not in workflow_services:
        print("LLM 未装配（缺 config/data.yaml 或 llm.yaml）", file=sys.stderr)
        sys.exit(1)
    llm_cfg = workflow_services["llm_config"]
    # 接入 events 写入器，让 chat.session.created 走 events 表
    sched_store = workflow_services.get("scheduler_state_store")
    event_writer = sched_store.write_event if sched_store is not None else None
    agent = ChatAgent(
        orchestrator=workflow_services["llm_orchestrator"],
        cfg=llm_cfg.chat,
        safety=llm_cfg.safety,
        store=workflow_services["llm_store"],
        event_writer=event_writer,
    )
    agent.repl()


def cmd_llm_calls(args: argparse.Namespace) -> None:
    """打印最近 LLM 调用记录。"""
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    from akq_agents.services.llm import LLMStore

    store = LLMStore(repo._base_dir / "meta.db")
    calls = store.list_calls(limit=args.last, agent=args.agent)
    rows = [
        {
            "id": c.id,
            "ts": c.ts,
            "agent": c.agent,
            "session_id": c.session_id,
            "model": c.model,
            "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens,
            "latency_ms": c.latency_ms,
            "tool_calls": c.tool_calls,
            "status": c.status,
            "reason_code": c.reason_code,
            "error_msg": c.error_msg,
        }
        for c in calls
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_llm_sessions(args: argparse.Namespace) -> None:
    """列出最近的 chat sessions。"""
    data_config = _require_data_config()
    repo = build_data_repository(data_config)
    from akq_agents.services.llm import LLMStore

    store = LLMStore(repo._base_dir / "meta.db")
    print(json.dumps(store.list_sessions(limit=args.last), ensure_ascii=False, indent=2))


# ---------------- P5 web ----------------


def cmd_web_start(args: argparse.Namespace) -> None:
    """启动 Web 控制台（前台阻塞，Ctrl+C 退出）。"""
    from akq_agents.bootstrap import load_web_config
    from akq_agents.web.server import start as web_start

    web_cfg = load_web_config()
    host = args.host or web_cfg.bind_host
    port = args.port or web_cfg.bind_port
    web_start(host=host, port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akq-agents", description="AKShare + akquant multi-agent quant system CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-once", help="Run the full pipeline once")
    run_parser.set_defaults(func=cmd_run)

    doctor_parser = subparsers.add_parser("doctor", help="Check environment readiness for real services")
    doctor_parser.set_defaults(func=cmd_doctor)

    data_parser = subparsers.add_parser("data", help="P1 data-layer commands")
    data_sub = data_parser.add_subparsers(dest="data_command", required=True)

    bootstrap_p = data_sub.add_parser("bootstrap", help="Backfill OHLCV history")
    bootstrap_p.add_argument("--lookback", type=int, default=None)
    bootstrap_p.set_defaults(func=cmd_data_bootstrap)

    refresh_p = data_sub.add_parser("refresh", help="Refresh a single day's OHLCV")
    refresh_p.add_argument("--date", default=None)
    refresh_p.set_defaults(func=cmd_data_refresh)

    status_p = data_sub.add_parser("status", help="Print DataHealth JSON")
    status_p.set_defaults(func=cmd_data_status)

    inspect_p = data_sub.add_parser("inspect", help="Inspect cached OHLCV for a symbol")
    inspect_p.add_argument("symbol")
    inspect_p.set_defaults(func=cmd_data_inspect)

    # P2 daemon subcommands
    daemon_parser = subparsers.add_parser("daemon", help="P2 scheduler daemon commands")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command", required=True)

    start_p = daemon_sub.add_parser("start", help="Start the daemon in foreground (Ctrl+C to stop)")
    start_p.set_defaults(func=cmd_daemon_start)

    status_p = daemon_sub.add_parser("status", help="Print daemon state JSON (reads daemon_state.json)")
    status_p.set_defaults(func=cmd_daemon_status)

    runs_p = daemon_sub.add_parser("runs", help="List recent job_runs")
    runs_p.add_argument("--last", type=int, default=20)
    runs_p.add_argument("--job-id", default=None)
    runs_p.add_argument("--status", default=None, help="ok|failed|skipped|timeout|crashed|interrupted|running")
    runs_p.set_defaults(func=cmd_daemon_runs)

    events_p = daemon_sub.add_parser("events", help="List recent events")
    events_p.add_argument("--last", type=int, default=20)
    events_p.add_argument("--level", default=None, choices=[None, "info", "warning", "error"], help="minimum level")
    events_p.add_argument("--kind-prefix", default=None, help="filter by kind prefix, e.g. batch.")
    events_p.set_defaults(func=cmd_daemon_events)

    # P3 factors subcommands
    factors_parser = subparsers.add_parser("factors", help="P3 factor registry commands")
    factors_sub = factors_parser.add_subparsers(dest="factors_command", required=True)

    flist_p = factors_sub.add_parser("list", help="List all registered factors with latest metrics")
    flist_p.set_defaults(func=cmd_factors_list)

    finspect_p = factors_sub.add_parser("inspect", help="Show historical metrics of a factor")
    finspect_p.add_argument("name")
    finspect_p.add_argument("--limit", type=int, default=30)
    finspect_p.set_defaults(func=cmd_factors_inspect)

    fdisc_p = factors_sub.add_parser("discover", help="Run one batch of auto factor discovery")
    fdisc_p.add_argument("-n", type=int, default=20, help="Number of candidates to sample")
    fdisc_p.set_defaults(func=cmd_factors_discover)

    fprop_p = factors_sub.add_parser("proposals", help="List recent factor proposals (auto-discovered)")
    fprop_p.add_argument("--limit", type=int, default=30)
    fprop_p.add_argument("--status", default=None, choices=[None, "accepted", "rejected", "pending"])
    fprop_p.set_defaults(func=cmd_factors_proposals)

    # P3 portfolio subcommands
    portfolio_parser = subparsers.add_parser("portfolio", help="P3 portfolio snapshot commands")
    portfolio_sub = portfolio_parser.add_subparsers(dest="portfolio_command", required=True)

    pexplain_p = portfolio_sub.add_parser("explain", help="Show portfolio snapshot for a date")
    pexplain_p.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today")
    pexplain_p.set_defaults(func=cmd_portfolio_explain)

    # L-5: trade-list / paper / holdings
    tl_p = subparsers.add_parser("trade-list", help="Show today BUY/SELL/HOLD action list")
    tl_p.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to latest")
    tl_p.set_defaults(func=cmd_trade_list)

    paper_p = subparsers.add_parser("paper", help="Paper Trading forward-track summary")
    paper_sub = paper_p.add_subparsers(dest="paper_command", required=True)
    psum_p = paper_sub.add_parser("summary", help="Aggregate cohort performance at 30/60/90 days")
    psum_p.set_defaults(func=cmd_paper_summary)

    hold_p = subparsers.add_parser("holdings", help="Show / set real holdings")
    hold_sub = hold_p.add_subparsers(dest="holdings_command", required=True)
    hlist_p = hold_sub.add_parser("list", help="List current real holdings")
    hlist_p.set_defaults(func=cmd_holdings_list)
    hset_p = hold_sub.add_parser("set", help="Set / update one holding")
    hset_p.add_argument("symbol")
    hset_p.add_argument("shares", type=float)
    hset_p.add_argument("--cost", default=None, help="avg cost")
    hset_p.add_argument("--note", default=None)
    hset_p.set_defaults(func=cmd_holdings_set)

    # P4 chat
    chat_p = subparsers.add_parser("chat", help="Start interactive LLM chat REPL")
    chat_p.set_defaults(func=cmd_chat)

    # P4 llm subcommands
    llm_parser = subparsers.add_parser("llm", help="P4 LLM history / sessions commands")
    llm_sub = llm_parser.add_subparsers(dest="llm_command", required=True)

    lcalls_p = llm_sub.add_parser("calls", help="List recent LLM calls")
    lcalls_p.add_argument("--last", type=int, default=20)
    lcalls_p.add_argument("--agent", default=None, choices=[None, "analyst", "chat"])
    lcalls_p.set_defaults(func=cmd_llm_calls)

    lsess_p = llm_sub.add_parser("sessions", help="List chat sessions (grouped from chat_messages)")
    lsess_p.add_argument("--last", type=int, default=20)
    lsess_p.set_defaults(func=cmd_llm_sessions)

    # P5 web
    web_parser = subparsers.add_parser("web", help="P5 Web console commands")
    web_sub = web_parser.add_subparsers(dest="web_command", required=True)

    wstart_p = web_sub.add_parser("start", help="Start the web console (foreground; Ctrl+C to stop)")
    wstart_p.add_argument("--host", default=None, help="Bind host (default from config; must be loopback)")
    wstart_p.add_argument("--port", type=int, default=None, help="Bind port (default from config)")
    wstart_p.set_defaults(func=cmd_web_start)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
