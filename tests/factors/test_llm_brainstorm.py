"""LLM factor brainstorm 相关测试。"""
from pathlib import Path
from unittest.mock import MagicMock

from akq_agents.services.factors.llm_brainstorm import build_state_summary
from akq_agents.services.factors.proposal_store import (
    FactorProposal, FactorProposalStore, now_iso,
)


def test_proposal_store_accepts_llm_suggested_status(tmp_path: Path) -> None:
    store = FactorProposalStore(tmp_path / "meta.db")
    p = FactorProposal(
        factor_name="llm_test_close_pct_change_5_long",
        recipe_json='{"base":"close","op":"pct_change","window":5,"direction":"long"}',
        direction="long",
        status="llm_suggested",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="LLM suggested: 短期动量在 high-volatility 区间有效",
        created_at=now_iso(),
        evaluated_at=None,
    )
    store.upsert(p)
    rows = store.list_recent(status="llm_suggested")
    assert len(rows) == 1
    assert rows[0].factor_name == "llm_test_close_pct_change_5_long"
    assert rows[0].status == "llm_suggested"


def test_build_state_summary_includes_dsl_and_stats(tmp_path: Path) -> None:
    registry = MagicMock()
    f1 = MagicMock(direction="long", lookback_days=60)
    f1.name = "momentum_20"
    f1.factor_version = 1
    f2 = MagicMock(direction="short", lookback_days=60)
    f2.name = "volatility_20"
    f2.factor_version = 1
    registry.list_all.return_value = [f1, f2]

    evaluator = MagicMock()
    latest = MagicMock(ic_mean=0.04, ir=0.45, window_days=60, as_of_date="2026-06-22")
    evaluator.get_latest.side_effect = lambda name, ver: latest if name == "momentum_20" else None

    store = FactorProposalStore(tmp_path / "meta.db")
    store.upsert(FactorProposal(
        factor_name="auto_zscore_close_20_long_a1b2",
        recipe_json='{"base":"close","op":"zscore","window":20,"direction":"long"}',
        direction="long", status="accepted",
        ic_mean=0.03, ic_std=0.5, ir=0.4, t_stat=2.1, max_abs_corr=0.5,
        reason="ok", created_at=now_iso(), evaluated_at=now_iso(),
    ))

    md = build_state_summary(registry, evaluator, store)

    assert "close" in md and "volume" in md
    assert "pct_change" in md and "zscore" in md
    assert "momentum_20" in md
    assert "auto_zscore_close_20_long_a1b2" in md
    assert 500 < len(md) < 8000
