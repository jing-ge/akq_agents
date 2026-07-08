"""Golden 等价回归：evaluate_batch_fast (增量 rolling IC) 必须与旧逐日 evaluate 路径
产出逐位相同的 FactorMetric。

背景 (方案 2 提速):
- 旧路径 backfill_one 对 ~90 个 as_of 逐个 evaluator.evaluate(), 每次 _rolling_ic 重算
  最后 60 天逐日 IC (相邻窗口 59 天重叠, ~97% 冗余)。
- 新路径 evaluate_batch_fast: 一次 _rolling_ic_full 算全历史 IC, 逐 as_of 取 tail(window)
  做 O(1) 聚合。
- 逐日 IC 只依赖当天截面 rank 相关, 与窗口/截止日无关 → 两路径数值严格等价。
  本测试是"零风险"的守门测试: 任何不等价 = 回滚信号。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.portfolio.evaluator import FactorEvaluator, FactorMetric


class _Factor:
    """最小 duck-typed Factor: evaluate 只用 name / factor_version。"""

    def __init__(self, name: str, version: int = 1) -> None:
        self.name = name
        self.factor_version = version


def _make_frames(n_days: int, n_symbols: int, seed: int, *, signal: float = 0.0):
    """构造确定性 factor_history + forward_returns。

    signal: 因子值与下一日收益的相关强度。0 = 纯噪声 (低 IC → 触发 low_ir 分支);
            大 = 强相关 (高 IR → active)。含 NaN 停牌行测健壮性。
    """
    rng = np.random.default_rng(seed)
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(n_days)]
    syms = [f"s{i}" for i in range(n_symbols)]
    fvals = rng.standard_normal((n_days, n_symbols))
    noise = rng.standard_normal((n_days, n_symbols))
    rvals = signal * fvals + noise
    fh = pd.DataFrame(fvals, index=pd.Index(dates), columns=syms)
    fr = pd.DataFrame(rvals, index=pd.Index(dates), columns=syms)
    # 插入几处 NaN (停牌) 测 dropna 路径一致
    fh.iloc[5, 0] = np.nan
    fr.iloc[7, 1] = np.nan
    return fh, fr


def _run_old(evaluator: FactorEvaluator, factor, fh, fr, as_of_dates):
    """旧路径: batch context 内逐个 evaluate (buffer 不 flush 到中途), 返回 metrics。"""
    out = []
    with evaluator.batch():
        for as_of in as_of_dates:
            m = evaluator.evaluate(
                factor=factor,
                factor_history=fh.loc[:as_of],
                forward_returns=fr.loc[:as_of],
                as_of_date=as_of,
            )
            out.append(m)
    return out


def _assert_metrics_equal(old, new):
    assert len(old) == len(new)
    for o, n in zip(old, new, strict=True):
        assert o.as_of_date == n.as_of_date
        assert o.status == n.status, f"status mismatch @ {o.as_of_date}: {o.status} vs {n.status}"
        assert o.reason == n.reason, f"reason mismatch @ {o.as_of_date}: {o.reason} vs {n.reason}"
        assert o.window_days == n.window_days
        for attr in ("ic_mean", "ic_std", "ir", "t_stat"):
            ov, nv = getattr(o, attr), getattr(n, attr)
            if ov is None or nv is None:
                assert ov is nv or (ov is None and nv is None), f"{attr} @ {o.as_of_date}: {ov} vs {nv}"
            else:
                assert nv == pytest.approx(ov, rel=1e-9, abs=1e-12), f"{attr} @ {o.as_of_date}: {ov} vs {nv}"


def _all_as_of(fh):
    # 覆盖: 起始 window 不足 (insufficient_data) + 正常段。
    return list(fh.index)


def test_equiv_high_signal_active(tmp_path):
    """强相关因子: 大部分 as_of active, 验证 ic_mean/ir/t_stat 逐位等价。"""
    fh, fr = _make_frames(n_days=90, n_symbols=8, seed=1, signal=3.0)
    as_of_dates = _all_as_of(fh)
    factor = _Factor("f_high")

    old = _run_old(FactorEvaluator(tmp_path / "old.db", window=60), factor, fh, fr, as_of_dates)
    new = FactorEvaluator(tmp_path / "new.db", window=60).evaluate_batch_fast(
        factor=factor, factor_history=fh, forward_returns=fr, as_of_dates=as_of_dates
    )
    _assert_metrics_equal(old, new)


def test_equiv_pure_noise_low_ir(tmp_path):
    """纯噪声因子: 触发 low_ir_observed / low_ir_persistent status 链, 验证 status 判定等价。"""
    fh, fr = _make_frames(n_days=90, n_symbols=8, seed=2, signal=0.0)
    as_of_dates = _all_as_of(fh)
    factor = _Factor("f_noise")

    old = _run_old(FactorEvaluator(tmp_path / "old.db", window=60), factor, fh, fr, as_of_dates)
    new = FactorEvaluator(tmp_path / "new.db", window=60).evaluate_batch_fast(
        factor=factor, factor_history=fh, forward_returns=fr, as_of_dates=as_of_dates
    )
    _assert_metrics_equal(old, new)
    # 确认确实走到了 low_ir 分支 (否则测试没覆盖到 status 逻辑)
    reasons = {m.reason for m in new if m.reason}
    assert any("low_ir" in r for r in reasons), f"expected low_ir status, got {reasons}"


def test_equiv_insufficient_data_prefix(tmp_path):
    """起始段 common_idx < window: 必须产出 insufficient_data metric, 两路径一致。"""
    fh, fr = _make_frames(n_days=40, n_symbols=6, seed=3, signal=1.0)  # < window=60
    as_of_dates = _all_as_of(fh)
    factor = _Factor("f_short")

    old = _run_old(FactorEvaluator(tmp_path / "old.db", window=60), factor, fh, fr, as_of_dates)
    new = FactorEvaluator(tmp_path / "new.db", window=60).evaluate_batch_fast(
        factor=factor, factor_history=fh, forward_returns=fr, as_of_dates=as_of_dates
    )
    _assert_metrics_equal(old, new)
    assert all(m.reason == "insufficient_data" for m in new)


def test_equiv_persisted_to_db(tmp_path):
    """新路径结束后 metrics 必须落库 (一次 _upsert_many), 行数与 as_of 数一致。"""
    import sqlite3

    fh, fr = _make_frames(n_days=90, n_symbols=8, seed=4, signal=2.0)
    as_of_dates = _all_as_of(fh)
    factor = _Factor("f_persist")
    dbp = tmp_path / "new.db"
    FactorEvaluator(dbp, window=60).evaluate_batch_fast(
        factor=factor, factor_history=fh, forward_returns=fr, as_of_dates=as_of_dates
    )
    conn = sqlite3.connect(dbp)
    n = conn.execute(
        "SELECT count(*) FROM factor_metrics WHERE factor_name='f_persist'"
    ).fetchone()[0]
    assert n == len(as_of_dates)


def test_equiv_with_preexisting_history(tmp_path):
    """增量场景: 两个 db 各预置相同的低 IR 历史, 再各跑一路径。

    验证 _read_recent_history 读到已有 db 数据时, low_ir_persistent 判定两路径一致
    (不只是空 db 首跑)。
    """
    fh, fr = _make_frames(n_days=90, n_symbols=8, seed=5, signal=0.0)  # 纯噪声 → 低 IR
    as_of_dates = _all_as_of(fh)
    factor = _Factor("f_incr")

    old_ev = FactorEvaluator(tmp_path / "old.db", window=60)
    new_ev = FactorEvaluator(tmp_path / "new.db", window=60)
    # 两个 db 预置相同的 3 期低 IR 历史 (早于 as_of_dates), 制造 low_ir_persistent 前置条件
    seed_dates = [date(2024, 12, 1) + timedelta(days=i) for i in range(3)]
    for ev in (old_ev, new_ev):
        seeded = [
            FactorMetric(
                factor_name="f_incr", factor_version=1,
                as_of_date=d.isoformat(), window_days=60,
                ic_mean=0.001, ic_std=0.5, ir=0.01, t_stat=0.05,
                status="active", reason="low_ir_observed_1/5",
            )
            for d in seed_dates
        ]
        ev._upsert_many(seeded)  # noqa: SLF001 — 测试直接写历史

    old = _run_old(old_ev, factor, fh, fr, as_of_dates)
    new = new_ev.evaluate_batch_fast(
        factor=factor, factor_history=fh, forward_returns=fr, as_of_dates=as_of_dates
    )
    _assert_metrics_equal(old, new)
