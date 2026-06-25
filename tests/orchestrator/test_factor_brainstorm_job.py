from unittest.mock import MagicMock

import pytest

from akq_agents.orchestrator.jobs.factor_brainstorm import _do


def test_do_delegates_to_brainstormer() -> None:
    brainstormer = MagicMock()
    brainstormer.run.return_value = {"requested": 5, "accepted_into_review": 3,
                                       "invalid": 1, "duplicate": 1, "errors": 0}
    services = {"llm_factor_brainstormer": brainstormer}
    out = _do(services, n=5)
    brainstormer.run.assert_called_once_with(n=5)
    assert out["accepted_into_review"] == 3


def test_do_raises_when_all_errors() -> None:
    """B2: LLM 全失败时必须 raise，避免 JobRunner 把全失败显示成 ok。"""
    brainstormer = MagicMock()
    brainstormer.run.return_value = {
        "requested": 5, "accepted_into_review": 0,
        "invalid": 0, "duplicate": 0, "errors": 5,
    }
    services = {"llm_factor_brainstormer": brainstormer}
    with pytest.raises(RuntimeError, match="brainstorm produced 0 proposals"):
        _do(services, n=5)


def test_do_passes_when_partial_failure() -> None:
    """B2: 部分失败但有接受的提议不应 raise（保留可用候选）。"""
    brainstormer = MagicMock()
    brainstormer.run.return_value = {
        "requested": 5, "accepted_into_review": 2,
        "invalid": 0, "duplicate": 1, "errors": 2,
    }
    services = {"llm_factor_brainstormer": brainstormer}
    out = _do(services, n=5)  # 不抛
    assert out["accepted_into_review"] == 2
