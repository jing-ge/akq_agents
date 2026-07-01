"""LLM factor brainstorm 相关测试。"""
from pathlib import Path
from unittest.mock import MagicMock

from akq_agents.services.factors.llm_brainstorm import (
    _parse_llm_response,
    build_state_summary,
)
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


from akq_agents.services.factors.llm_brainstorm import (
    LLMFactorBrainstormer, _validate_recipe, _recipe_to_name,
)


def test_validate_recipe_accepts_valid() -> None:
    assert _validate_recipe({
        "base": "close", "op": "pct_change", "window": 5, "direction": "long",
    }) is None


def test_validate_recipe_rejects_unknown_op() -> None:
    err = _validate_recipe({
        "base": "close", "op": "ema",
        "window": 5, "direction": "long",
    })
    assert err is not None and "op" in err


def test_validate_recipe_rejects_unknown_window() -> None:
    err = _validate_recipe({
        "base": "close", "op": "pct_change", "window": 7,
        "direction": "long",
    })
    assert err is not None and "window" in err


def test_recipe_to_name_is_deterministic() -> None:
    r = {"base": "close", "op": "zscore", "window": 30, "direction": "long"}
    assert _recipe_to_name(r) == _recipe_to_name(r)
    assert _recipe_to_name(r).startswith("llm_")


def test_brainstormer_writes_valid_suggestions_to_store(tmp_path: Path) -> None:
    llm_client = MagicMock()
    llm_resp = MagicMock()
    llm_resp.text = '''
    {"suggestions": [
        {"recipe": {"base":"close","op":"zscore","window":30,"direction":"long"},
         "rationale": "中期 zscore 动量"},
        {"recipe": {"base":"close","op":"ema","window":10,"direction":"long"},
         "rationale": "新算子（不合法）"}
    ]}
    '''
    llm_resp.prompt_tokens = 100
    llm_resp.completion_tokens = 50
    llm_client.chat.return_value = llm_resp

    store = FactorProposalStore(tmp_path / "meta.db")
    registry = MagicMock(list_all=MagicMock(return_value=[]))
    evaluator = MagicMock(get_latest=MagicMock(return_value=None))

    brainstormer = LLMFactorBrainstormer(
        llm_client=llm_client,
        proposal_store=store,
        registry=registry,
        evaluator=evaluator,
        model="test-model",
        max_tokens=2000,
        temperature=1.0,
    )

    stats = brainstormer.run(n=2)

    assert stats["requested"] == 2
    assert stats["accepted_into_review"] == 1
    assert stats["invalid"] == 1

    rows = store.list_recent(status="llm_suggested")
    assert len(rows) == 1
    assert "zscore" in rows[0].recipe_json
    assert "中期 zscore 动量" in (rows[0].reason or "")


def test_brainstormer_skips_duplicate_recipe(tmp_path: Path) -> None:
    """同一 recipe 第二次提议应被识别为重复跳过。"""
    store = FactorProposalStore(tmp_path / "meta.db")
    store.upsert(FactorProposal(
        factor_name=_recipe_to_name({"base":"close","op":"zscore","window":30,"direction":"long"}),
        recipe_json='{"base":"close","op":"zscore","window":30,"direction":"long"}',
        direction="long", status="rejected",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="too low IR", created_at=now_iso(), evaluated_at=now_iso(),
    ))

    llm_client = MagicMock()
    llm_resp = MagicMock(
        text='{"suggestions":[{"recipe":{"base":"close","op":"zscore","window":30,"direction":"long"},"rationale":"x"}]}',
        prompt_tokens=10, completion_tokens=10,
    )
    llm_client.chat.return_value = llm_resp

    registry = MagicMock(list_all=MagicMock(return_value=[]))
    evaluator = MagicMock(get_latest=MagicMock(return_value=None))

    brainstormer = LLMFactorBrainstormer(
        llm_client=llm_client, proposal_store=store,
        registry=registry, evaluator=evaluator,
        model="test-model", max_tokens=2000, temperature=1.0,
    )
    stats = brainstormer.run(n=1)
    assert stats["duplicate"] == 1
    assert stats["accepted_into_review"] == 0


def test_parse_llm_response_recovers_from_truncation():
    """LLM 输出被 max_tokens 截断时应回退到"提取前 N 个完整对象"，
    而不是整包 JSONDecodeError 爆掉。"""
    # 模拟：3 个完整 suggestion + 第 4 个截断在 rationale 中间
    truncated = (
        '{\n'
        '  "suggestions": [\n'
        '    {"recipe": {"base":"close","op":"pct_change","window":5,"direction":"long"}, "rationale": "短期动量"},\n'
        '    {"recipe": {"base":"amount","op":"rolling_mean","window":20,"direction":"long"}, "rationale": "流动性"},\n'
        '    {"recipe": {"base":"vwap","op":"rsi","window":10,"direction":"short"}, "rationale": "反转"},\n'
        '    {"recipe": {"base":"close","op":"zscore","window":5,"direction":"lo'
        # 到这里就断了，没有 ", "rationale": ..., "]", "}" 三层收尾
    )
    got = _parse_llm_response(truncated)
    # 应该救回前 3 个完整的
    assert len(got) == 3
    assert got[0]["recipe"]["op"] == "pct_change"
    assert got[2]["recipe"]["op"] == "rsi"


def test_parse_llm_response_normal_still_works():
    """回归：完整 JSON 应该走原路径，不受截断兜底影响。"""
    complete = '{"suggestions": [{"recipe": {"base": "close", "op": "pct_change", "window": 5, "direction": "long"}, "rationale": "test"}]}'
    got = _parse_llm_response(complete)
    assert len(got) == 1
    assert got[0]["recipe"]["window"] == 5


def test_proposal_store_upsert_persists_direction_flip(tmp_path: Path) -> None:
    """F2 regression: promote 时 OOS IR<0 会 flip direction 并改写 recipe_json,
    upsert 必须把这两个字段落库, 否则 daemon 重启 restore_accepted_factors 读到老 recipe,
    flip 静默失效。"""
    store = FactorProposalStore(tmp_path / "meta.db")

    # 1) 初始入库 long 方向
    p = FactorProposal(
        factor_name="llm_test_flip_direction",
        recipe_json='{"base":"close","op":"pct_change","window":5,"direction":"long"}',
        direction="long",
        status="shadow",
        ic_mean=0.01, ic_std=0.05, ir=0.20, t_stat=2.5, max_abs_corr=0.30,
        reason="initial",
        created_at=now_iso(),
        evaluated_at=None,
    )
    store.upsert(p)

    # 2) promote 时 flip: 改 recipe_json/direction 后再 upsert
    p.recipe_json = '{"base":"close","op":"pct_change","window":5,"direction":"short"}'
    p.direction = "short"
    p.status = "accepted"
    p.reason = "promoted after flip"
    store.upsert(p)

    # 3) 读回来必须是 short (之前 bug: 只 update status, recipe/direction 保持老值)
    rows = store.list_recent(status="accepted")
    got = next(r for r in rows if r.factor_name == "llm_test_flip_direction")
    assert got.direction == "short", f"direction 落库丢失: 实际 {got.direction!r}"
    assert '"direction":"short"' in got.recipe_json, \
        f"recipe_json 落库丢失: {got.recipe_json}"

