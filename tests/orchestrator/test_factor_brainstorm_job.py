from unittest.mock import MagicMock

from akq_agents.orchestrator.jobs.factor_brainstorm import _do


def test_do_delegates_to_brainstormer() -> None:
    brainstormer = MagicMock()
    brainstormer.run.return_value = {"requested": 5, "accepted_into_review": 3,
                                       "invalid": 1, "duplicate": 1, "errors": 0}
    services = {"llm_factor_brainstormer": brainstormer}
    out = _do(services, n=5)
    brainstormer.run.assert_called_once_with(n=5)
    assert out["accepted_into_review"] == 3
