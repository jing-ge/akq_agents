"""LLM factor brainstorm 相关测试。"""
from pathlib import Path

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
