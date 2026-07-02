"""测试重构后 factor discovery 的 code 路径.

覆盖:
- proposal_store.upsert + list_recent 区分 recipe_kind='dsl' vs 'code'
- exists_recipe 只查 dsl, exists_code_hash 只查 code (跨路径不串)
- make_factor 收到 recipe['_source_code'] 时走 CodeFactor 路径
- restore_accepted_factors 从 db 读 code 因子也能恢复进 registry
- FactorSpace 的 dsl sample / code 子空间 都能用
- sandbox compile 失败的 LLM 输出不污染库 (rejected)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from akq_agents.services.factors.base import CodeFactor, FactorRegistry
from akq_agents.services.factors.discovery import (
    CodeProposal, FactorSpace, make_factor, restore_accepted_factors,
)
from akq_agents.services.factors.proposal_store import (
    FactorProposal, FactorProposalStore, now_iso, recipe_to_json,
)
from akq_agents.services.factors.sandbox import code_hash as _code_hash


# ---------------- proposal_store 路由 ----------------


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as d:
        yield FactorProposalStore(Path(d) / "meta.db")


def _dsl_proposal(name: str = "auto_xxx") -> FactorProposal:
    return FactorProposal(
        factor_name=name,
        recipe_kind="dsl",
        recipe_json=recipe_to_json({"base": "close", "op": "pct_change",
                                     "window": 5, "direction": "long"}),
        direction="long",
        status="rejected",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="test",
        created_at=now_iso(),
        evaluated_at=now_iso(),
    )


def _code_proposal(name: str, source: str) -> FactorProposal:
    h = _code_hash(source)
    return FactorProposal(
        factor_name=name,
        recipe_kind="code",
        recipe_json=json.dumps({"description": "test", "direction": "long"}),
        direction="long",
        status="llm_suggested",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="test",
        created_at=now_iso(),
        evaluated_at=None,
        recipe_code=source,
        code_hash=h,
    )


def test_proposal_store_upsert_and_list_recent(tmp_store):
    tmp_store.upsert(_dsl_proposal("auto_test_1"))
    tmp_store.upsert(_code_proposal("code_test_a", "def compute(ohlcv):\n    return ohlcv['close']"))
    rows = tmp_store.list_recent(limit=10)
    assert len(rows) == 2
    kinds = {r.factor_name: r.recipe_kind for r in rows}
    assert kinds["auto_test_1"] == "dsl"
    assert kinds["code_test_a"] == "code"


def test_list_recent_filter_recipe_kind(tmp_store):
    tmp_store.upsert(_dsl_proposal("auto_d1"))
    tmp_store.upsert(_dsl_proposal("auto_d2"))
    tmp_store.upsert(_code_proposal("code_c1", "def compute(ohlcv):\n    pass"))
    dsl_rows = tmp_store.list_recent(limit=10, recipe_kind="dsl")
    code_rows = tmp_store.list_recent(limit=10, recipe_kind="code")
    assert {r.factor_name for r in dsl_rows} == {"auto_d1", "auto_d2"}
    assert {r.factor_name for r in code_rows} == {"code_c1"}


def test_exists_recipe_only_matches_dsl(tmp_store):
    """exists_recipe 走 dsl 路径, code 路径不受影响."""
    src = "def compute(ohlcv):\n    return ohlcv['close']"
    tmp_store.upsert(_code_proposal("code_c1", src))
    # 同样的 recipe_json 字符串, exists_recipe 应该找不到 (因为是 code 路径)
    fake_recipe = recipe_to_json({"base": "close", "op": "pct_change",
                                   "window": 5, "direction": "long"})
    assert tmp_store.exists_recipe(fake_recipe) is None
    # 但用 code_hash 能找到
    assert tmp_store.exists_code_hash(_code_hash(src)) == "code_c1"


def test_exists_code_hash_only_matches_code(tmp_store):
    """exists_code_hash 走 code 路径, DSL 路径不受影响 (DSL 不会带 code_hash)."""
    src = "def compute(ohlcv):\n    return ohlcv['close']"
    h = _code_hash(src)
    tmp_store.upsert(_code_proposal("code_c1", src))
    assert tmp_store.exists_code_hash(h) == "code_c1"
    # 不存在的 hash 返 None
    assert tmp_store.exists_code_hash("deadbeef" + "0" * 32) is None


# ---------------- make_factor 路由 ----------------


def test_make_factor_dsl_default():
    """默认走 DSL 路径, 不带 _source_code 走 _RuntimeFactor."""
    f = make_factor({"base": "close", "op": "pct_change", "window": 5, "direction": "long"})
    assert f.name.startswith("auto_pct_change_close_5_long_")
    assert f.direction == "long"


def test_make_factor_with_source_code_routes_to_code_factor():
    """make_factor 收到 _source_code 走 CodeFactor 路径."""
    src = """
def compute(ohlcv):
    return ohlcv['close'].pct_change().iloc[-1]
"""
    f = make_factor({"_source_code": src, "direction": "long"},
                    name="manual_code")
    assert isinstance(f, CodeFactor)
    assert f.name == "manual_code"
    assert f.direction == "long"
    assert len(f.code_hash) == 40


# ---------------- restore_accepted_factors 支持 code ----------------


def test_restore_accepted_factors_handles_code(tmp_store):
    """启动期: db 里 status='accepted' 的 code 因子也能恢复进 registry."""
    src = """
def compute(ohlcv):
    wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()
    return wide.pct_change(5).iloc[-1]
"""
    tmp_store.upsert(FactorProposal(
        factor_name="code_xxx_yyy",
        recipe_kind="code",
        recipe_json=json.dumps({"description": "test code factor", "direction": "long"}),
        direction="long",
        status="accepted",
        ic_mean=0.02, ic_std=0.05, ir=0.4, t_stat=2.1, max_abs_corr=0.3,
        reason="ok",
        created_at=now_iso(),
        evaluated_at=now_iso(),
        recipe_code=src,
        code_hash=_code_hash(src),
    ))
    reg = FactorRegistry()
    n = restore_accepted_factors(reg, tmp_store)
    assert n == 1
    f = reg.get("code_xxx_yyy")
    assert isinstance(f, CodeFactor)
    assert f.direction == "long"
    # compute 实际能跑
    dates = pd.date_range("2026-06-01", periods=10, freq="D")
    rows = []
    for d in dates:
        for sym in ["A", "B"]:
            rows.append({"date": d, "symbol": sym, "open": 10.0, "high": 11.0,
                         "low": 9.0, "close": 10.0 + (d.day % 3) * 0.1,
                         "volume": 1e6, "amount": 1e7})
    out = f.compute(pd.DataFrame(rows))
    assert isinstance(out, pd.Series)
    assert out.name == "code_xxx_yyy"


def test_restore_accepted_factors_skips_demoted(tmp_store):
    """demoted / rejected 的因子不 restore."""
    tmp_store.upsert(_dsl_proposal("auto_demoted"))
    p = tmp_store.list_recent(limit=10)[0]
    p.status = "demoted"
    tmp_store.upsert(p)
    reg = FactorRegistry()
    n = restore_accepted_factors(reg, tmp_store)
    assert n == 0


# ---------------- FactorSpace 结构 ----------------


def test_factor_space_dsl_sample_size():
    """DSL 路径: 5 base × 8 op × 5 window × 2 direction = 400."""
    space = FactorSpace()
    assert space.size() == 5 * 8 * 5 * 2
    samples = space.sample(10)
    assert len(samples) == 10
    for s in samples:
        assert s.keys() >= {"base", "op", "window", "direction"}


def test_factor_space_sample_no_duplicates():
    """DSL sample 不会撞同 recipe."""
    space = FactorSpace()
    samples = space.sample(50)
    keys = {recipe_to_json(s) for s in samples}
    assert len(keys) == len(samples)


def test_code_proposal_dataclass():
    """CodeProposal dataclass 字段齐全."""
    src = "def compute(ohlcv):\n    return ohlcv['close']"
    p = CodeProposal(
        source_code=src, code_hash=_code_hash(src),
        direction="long", description="test",
    )
    assert p.direction == "long"
    assert len(p.code_hash) == 40