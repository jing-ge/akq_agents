"""PortfolioSnapshotStore 单元测试。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from akq_agents.services.portfolio.attributor import AttributionResult
from akq_agents.services.portfolio.snapshot_store import PortfolioSnapshotStore


def _empty_attribution() -> AttributionResult:
    return AttributionResult(
        as_of_date="2026-06-17",
        portfolio_contribution={},
        per_stock={},
        summary="",
    )


def test_write_basic(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    weights = pd.Series({"600519": 0.5, "000001": 0.5})
    scores = pd.Series({"600519": 1.2, "000001": 0.8})
    attribution = AttributionResult(
        as_of_date="2026-06-17",
        portfolio_contribution={"momentum_5": 0.3},
        per_stock={
            "600519": [{"name": "momentum_5", "contribution": 0.7}],
            "000001": [{"name": "momentum_5", "contribution": 0.4}],
        },
        summary="",
    )
    n = store.write(
        as_of_date=date(2026, 6, 17),
        weights=weights,
        composite_score=scores,
        attribution=attribution,
        name_map={"600519": "贵州茅台", "000001": "平安银行"},
    )
    assert n == 2
    rows = store.read_snapshot(date(2026, 6, 17))
    assert len(rows) == 2
    by_sym = {r.symbol: r for r in rows}
    assert by_sym["600519"].name == "贵州茅台"
    assert by_sym["600519"].weight == 0.5
    assert by_sym["600519"].composite_score == 1.2
    # top_factors_json 是合法 JSON 数组
    decoded = json.loads(by_sym["600519"].top_factors_json or "[]")
    assert decoded[0]["name"] == "momentum_5"


def test_write_idempotent_same_day(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    weights = pd.Series({"A": 1.0})
    scores = pd.Series({"A": 1.0})
    attr = _empty_attribution()
    store.write(as_of_date=date(2026, 6, 17), weights=weights, composite_score=scores, attribution=attr)
    # 再写一次同一日同一 symbol → upsert
    weights2 = pd.Series({"A": 0.6})
    store.write(as_of_date=date(2026, 6, 17), weights=weights2, composite_score=scores, attribution=attr)
    rows = store.read_snapshot(date(2026, 6, 17))
    assert len(rows) == 1
    assert rows[0].weight == 0.6


def test_read_prev_weights_skip_today(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    # 写 6-15 和 6-16
    for d, w in [(date(2026, 6, 15), 0.3), (date(2026, 6, 16), 0.5)]:
        store.write(
            as_of_date=d,
            weights=pd.Series({"A": w}),
            composite_score=pd.Series({"A": 1.0}),
            attribution=_empty_attribution(),
        )
    # 取 6-17 之前最近的一日 = 6-16
    prev = store.read_prev_weights(date(2026, 6, 17))
    assert prev.to_dict() == {"A": 0.5}


def test_read_prev_weights_empty_when_no_history(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    prev = store.read_prev_weights(date(2026, 6, 17))
    assert prev.empty


def test_write_new_symbol_prev_weight_zero(tmp_path: Path) -> None:
    """新股：prev_weights 不含该 symbol → 落库时 prev_weight=0。"""
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series({"NEW": 0.5}),
        composite_score=pd.Series({"NEW": 1.0}),
        attribution=_empty_attribution(),
        prev_weights=pd.Series({"OLD": 0.3}),  # OLD 不在新组合，NEW 没历史
    )
    rows = store.read_snapshot(date(2026, 6, 17))
    assert rows[0].prev_weight == 0.0


def test_list_dates_returns_desc(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    for d in [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]:
        store.write(
            as_of_date=d, weights=pd.Series({"A": 1.0}),
            composite_score=pd.Series({"A": 1.0}),
            attribution=_empty_attribution(),
        )
    dates = store.list_dates(limit=10)
    assert dates == ["2026-06-17", "2026-06-16", "2026-06-15"]


def test_read_snapshot_orders_by_weight_desc(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series({"SMALL": 0.1, "BIG": 0.5, "MID": 0.3}),
        composite_score=pd.Series({"SMALL": 1.0, "BIG": 2.0, "MID": 1.5}),
        attribution=_empty_attribution(),
    )
    rows = store.read_snapshot(date(2026, 6, 17))
    assert [r.symbol for r in rows] == ["BIG", "MID", "SMALL"]


def test_write_empty_weights_returns_zero(tmp_path: Path) -> None:
    store = PortfolioSnapshotStore(tmp_path / "meta.db")
    n = store.write(
        as_of_date=date(2026, 6, 17),
        weights=pd.Series(dtype=float),
        composite_score=pd.Series(dtype=float),
        attribution=_empty_attribution(),
    )
    assert n == 0
