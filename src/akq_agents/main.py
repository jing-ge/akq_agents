from __future__ import annotations

from akq_agents.bootstrap import build_workflow
from akq_agents.cli.app import main as cli_main


def run_once():
    workflow, _ = build_workflow()
    return workflow.run_once_and_print()


def main():
    cli_main()


if __name__ == "__main__":
    main()
