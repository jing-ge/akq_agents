"""回归测试：batch.deep_research 的 job_steps 必须写到 runner 用的 partition。

Bug 背景 (partition 不匹配):
- 写入侧 recorder 曾硬编码 ``parent_partition=date.today().isoformat()``。
- manual 触发的真实 partition 是 ``{date}-manual-{hex6}`` (control._manual_partition)。
- UI 用真实 partition 去 StepReader.list_steps → 永远 0 行 → "job_steps 为空"。
修复: _do(services, partition=...) 透传真实 partition, recorder 用它。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from akq_agents.orchestrator.jobs.batch_deep_research import _do
from akq_agents.orchestrator.step_recorder import StepReader
from akq_agents.services.factors.base import CodeFactor, FactorRegistry
from akq_agents.services.portfolio.evaluator import FactorEvaluator

# HistoryBackfillContext.from_existing 要求 close.index 去重 >= window + days = 60 + 90。
_N_DAYS = 60 + 90 + 5
_SYMBOLS = ["000001", "000002", "000003"]


def _build_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=_N_DAYS).date
    rows = []
    for d in dates:
        for i, s in enumerate(_SYMBOLS):
            rows.append({
                "date": d, "symbol": s,
                "open": 1.0, "high": 1.0, "low": 1.0,
                "close": 10.0 + i, "volume": 100.0, "amount": 1000.0 * (i + 1),
            })
    return pd.DataFrame(rows)


def _make_services(tmp_path):
    meta = tmp_path / "meta.db"
    ohlcv = _build_ohlcv()

    class _Universe:
        symbols = list(_SYMBOLS)

    class _FakeRepo:
        _base_dir = tmp_path
        _calendar = None
        meta_db_path = meta

        def get_universe(self, _d):
            return _Universe()

        def get_ohlcv_loose(self, _syms, _start, _end):
            return ohlcv.copy()

    reg = FactorRegistry()
    reg.register(CodeFactor(
        name="f1",
        source_code="",
        fn=lambda o: pd.Series({s: float(i) for i, s in enumerate(o["symbol"].unique())}),
        lookback_days=5,
    ))

    services = {
        "data_repository": _FakeRepo(),
        "factor_registry": reg,
        "factor_evaluator": FactorEvaluator(meta, window=60),
    }
    return services, meta


def test_do_records_steps_under_passed_manual_partition(tmp_path):
    """核心回归: 传 manual 风格 partition, 用同一 partition 必须读到 job_steps。"""
    services, meta = _make_services(tmp_path)
    manual_partition = f"{date.today().isoformat()}-manual-bd72ae"

    _do(services, mode="full", partition=manual_partition)

    steps = StepReader(meta).list_steps("batch.deep_research", manual_partition)
    assert len(steps) >= 1, "传入 partition 时 job_steps 必须写到该 partition"


def test_do_does_not_leak_steps_to_bare_date_partition(tmp_path):
    """传 manual partition 时, 不应把 step 写到裸 date partition (旧 bug 的表现)。"""
    services, meta = _make_services(tmp_path)
    manual_partition = f"{date.today().isoformat()}-manual-bd72ae"

    _do(services, mode="full", partition=manual_partition)

    leaked = StepReader(meta).list_steps("batch.deep_research", date.today().isoformat())
    assert leaked == [], "step 不应泄漏到裸 date partition"


def test_do_partition_defaults_to_today_when_omitted(tmp_path):
    """向后兼容: 不传 partition 时回退 date.today()。"""
    services, meta = _make_services(tmp_path)

    _do(services, mode="full")

    steps = StepReader(meta).list_steps("batch.deep_research", date.today().isoformat())
    assert len(steps) >= 1, "缺省应回退到 date.today() partition"
