from __future__ import annotations

import argparse
from pprint import pprint

from akq_agents.bootstrap import BASE_DIR, build_workflow
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

    with open(state_path, "r", encoding="utf-8") as file:
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

    with open(state_path, "r", encoding="utf-8") as file:
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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
