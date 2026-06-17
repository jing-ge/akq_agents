from __future__ import annotations

from akq_agents.bootstrap import build_workflow
from akq_agents.cli.app import main as cli_main
from akq_agents.orchestrator.scheduler import QuantScheduler


def run_once():
    workflow, _ = build_workflow()
    return workflow.run_once_and_print()


def run_scheduler():
    workflow, config = build_workflow()
    scheduler = QuantScheduler(config, workflow)
    scheduler.start()


def main():
    cli_main()


if __name__ == "__main__":
    main()
