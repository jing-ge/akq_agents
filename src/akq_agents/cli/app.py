from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pprint import pprint

from akq_agents.bootstrap import BASE_DIR, build_data_repository, build_workflow, load_data_config
from akq_agents.models.data_config import DataConfig
from akq_agents.services.data.exceptions import DataNotReady
from akq_agents.services.data.repository import DataRepository
from akq_agents.services.environment import EnvironmentDoctor
from akq_agents.services.notifier import NotificationService
from akq_agents.services.report_exporter import ReportExporter
from akq_agents.services.storage import SQLiteStore


def cmd_run(_: argparse.Namespace) -> None:
    workflow, _ = build_workflow()
    workflow.run_once_and_print()


def cmd_query(args: argparse.Namespace) -> None:
    store = SQLiteStore(str(BASE_DIR / "akq_agents.db"))
    if args.section in {"advice", "all"}:
        print("=== Latest Advice ===")
        for row in store.latest_advice():
            print(row["ts"])
            print(row["rendered"])
            print()
    if args.section in {"backtest", "all"}:
        print("=== Latest Backtests ===")
        for row in store.latest_backtest_reports(limit=args.limit):
            print(row)
    if args.section in {"portfolio", "all"}:
        print("\n=== Latest Portfolio ===")
        for row in store.latest_portfolio(limit=args.limit):
            print(row)


def cmd_analyze(_: argparse.Namespace) -> None:
    store = SQLiteStore(str(BASE_DIR / "akq_agents.db"))
    print("=== Factor Scoreboard ===")
    rows = store.query(
        """
        SELECT factor_name,
               COUNT(*) AS sample_count,
               ROUND(AVG(score), 6) AS avg_score,
               ROUND(AVG(sharpe), 6) AS avg_sharpe,
               ROUND(AVG(annual_return), 6) AS avg_annual_return,
               ROUND(AVG(max_drawdown), 6) AS avg_max_drawdown
        FROM backtest_reports
        GROUP BY factor_name
        ORDER BY avg_score DESC
        """
    )
    for row in rows:
        print(row)

    print("\n=== Portfolio Exposure ===")
    rows = store.query(
        """
        SELECT symbol,
               COUNT(*) AS appear_count,
               ROUND(AVG(weight), 6) AS avg_weight,
               ROUND(AVG(score), 6) AS avg_score
        FROM portfolio_recommendations
        GROUP BY symbol
        ORDER BY avg_score DESC
        """
    )
    for row in rows:
        print(row)


def cmd_export(args: argparse.Namespace) -> None:
    state_path = BASE_DIR / "runtime_state.yaml"
    import yaml

    with open(state_path, encoding="utf-8") as file:
        state = yaml.safe_load(file) or {}
    exporter = ReportExporter()
    exported = exporter.export_latest(
        state.get("latest_report"),
        state.get("latest_report_html"),
        args.output_dir,
    )
    pprint(exported)


def cmd_notify(args: argparse.Namespace) -> None:
    state_path = BASE_DIR / "runtime_state.yaml"
    import yaml

    with open(state_path, encoding="utf-8") as file:
        state = yaml.safe_load(file) or {}
    notifier = NotificationService()
    result = notifier.notify_stub(
        title=args.title,
        markdown_path=state.get("latest_report"),
        html_path=state.get("latest_report_html"),
        output_file=args.output_file,
    )
    pprint(result)


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
    repo.bootstrap_history(lookback_days=lookback_days, progress_cb=lambda done, total: print(f"[{done}/{total}] done"))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akq-agents", description="AKShare + akquant multi-agent quant system CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-once", help="Run the full pipeline once")
    run_parser.set_defaults(func=cmd_run)

    query_parser = subparsers.add_parser("query", help="Query latest results from SQLite")
    query_parser.add_argument("--section", choices=["all", "advice", "backtest", "portfolio"], default="all")
    query_parser.add_argument("--limit", type=int, default=5)
    query_parser.set_defaults(func=cmd_query)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze factor and portfolio history")
    analyze_parser.set_defaults(func=cmd_analyze)

    export_parser = subparsers.add_parser("export-report", help="Copy latest reports to an export directory")
    export_parser.add_argument("--output-dir", default=str(BASE_DIR / "exports"))
    export_parser.set_defaults(func=cmd_export)

    notify_parser = subparsers.add_parser("notify-stub", help="Write a notification preview file")
    notify_parser.add_argument("--title", default="AKQ Daily Report")
    notify_parser.add_argument("--output-file", default=str(BASE_DIR / "exports" / "notification_preview.txt"))
    notify_parser.set_defaults(func=cmd_notify)

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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
