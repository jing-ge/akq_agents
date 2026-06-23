"""PortfolioAgent (P3a) 端到端集成测试。

注入完整 P3 services 字典；验证：
- 跑通 7 个步骤
- portfolio_snapshots 表写入正确
- context.state 含 portfolio / attribution / turnover
- 失败路径（DataNotReady）返回 skipped
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from akq_agents.agents.base import AgentContext
from akq_agents.agents.portfolio_agent import PortfolioAgent
from akq_agents.services.data.exceptions import DataNotReady
from akq_agents.services.data.schemas import UniverseSnapshot
from akq_agents.services.factors import FactorEngine, build_default_registry
from akq_agents.services.portfolio import (
    Attributor,
    CompositeScorer,
    OptimizerConfig,
    PortfolioOptimizer,
    PortfolioSnapshotStore,
    Preprocessor,
)


def _make_ohlcv(n_days: int = 80, n_symbols: int = 100) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rows = []
    dates = pd.date_range("2026-04-01", periods=n_days)
    for sym_idx in range(n_symbols):
        sym = f"S{sym_idx:03d}"
        base = 10.0 + sym_idx * 0.1
        prices = base * (1 + rng.normal(0, 0.01, n_days)).cumprod()
        amts = rng.lognormal(mean=15, sigma=1.5, size=n_days)  # 跨多个数量级
        for i, ts in enumerate(dates):
            rows.append(
                {
                    "date": ts.date(),
                    "symbol": sym,
                    "open": prices[i] * 0.99,
                    "high": prices[i] * 1.01,
                    "low": prices[i] * 0.98,
                    "close": prices[i],
                    "volume": amts[i] / prices[i],
                    "amount": amts[i],
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def repo_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def services(tmp_path: Path, repo_mock: MagicMock) -> dict:
    registry = build_default_registry()
    return {
        "data_repository": repo_mock,
        "factor_registry": registry,
        "factor_engine": FactorEngine(),
        "preprocessor": Preprocessor(),
        "composite_scorer": CompositeScorer(),
        "portfolio_optimizer": PortfolioOptimizer(OptimizerConfig(top_n=10, max_single_weight=0.2)),
        "attributor": Attributor(),
        "portfolio_snapshot_store": PortfolioSnapshotStore(tmp_path / "meta.db"),
    }


def test_run_p3_end_to_end_writes_snapshot(services: dict, repo_mock: MagicMock) -> None:
    ohlcv = _make_ohlcv(n_days=80, n_symbols=100)
    symbols = sorted(ohlcv["symbol"].unique().tolist())
    repo_mock.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 17), symbols=symbols, excluded={}
    )
    repo_mock.get_ohlcv.return_value = ohlcv

    agent = PortfolioAgent(top_n_symbols=50, services=services)
    ctx = AgentContext(state={"today": "2026-06-17"})
    result = agent.run(ctx)

    assert result["status"] == "ok"
    assert result["portfolio_size"] > 0
    assert result["portfolio_size"] <= 10  # top_n=10
    assert result["as_of_date"] == "2026-06-17"
    assert result["turnover"] == pytest.approx(1.0)  # 首日

    # context 含 portfolio / attribution
    assert "portfolio" in ctx.state
    assert len(ctx.state["portfolio"]) == result["portfolio_size"]
    assert "attribution" in ctx.state
    assert "portfolio_contribution" in ctx.state["attribution"]
    assert ctx.state["portfolio_turnover"] == result["turnover"]

    # snapshot 表里有数据
    store = services["portfolio_snapshot_store"]
    rows = store.read_snapshot(date(2026, 6, 17))
    assert len(rows) == result["portfolio_size"]
    # 每行包含 top_factors_json
    import json
    assert all(json.loads(r.top_factors_json or "[]") for r in rows)


def test_run_p3_skipped_on_data_not_ready_universe(services: dict, repo_mock: MagicMock) -> None:
    repo_mock.get_universe.side_effect = DataNotReady({"_universe": [date(2026, 6, 17)]})
    result = PortfolioAgent(services=services).run(AgentContext(state={"today": "2026-06-17"}))
    assert result["status"] == "skipped"
    assert result["reason"] == "data_not_ready"


def test_run_p3_skipped_on_data_not_ready_ohlcv(services: dict, repo_mock: MagicMock) -> None:
    repo_mock.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 17), symbols=["S001"], excluded={}
    )
    repo_mock.get_ohlcv.side_effect = DataNotReady({"S001": [date(2026, 6, 17)]})
    result = PortfolioAgent(services=services).run(AgentContext(state={"today": "2026-06-17"}))
    assert result["status"] == "skipped"
    assert result["reason"] == "ohlcv_not_ready"



def test_run_p3_writes_turnover_in_state(services: dict, repo_mock: MagicMock) -> None:
    """两次连续跑 → 第二次 turnover < 1.0。"""
    ohlcv = _make_ohlcv(n_days=80, n_symbols=50)
    symbols = sorted(ohlcv["symbol"].unique().tolist())
    repo_mock.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 17), symbols=symbols, excluded={}
    )
    repo_mock.get_ohlcv.return_value = ohlcv

    agent = PortfolioAgent(services=services)
    # 6-17 跑一次
    result1 = agent.run(AgentContext(state={"today": "2026-06-17"}))
    assert result1["turnover"] == pytest.approx(1.0)
    # 6-18 跑一次（mock universe 已是同一天，但 PortfolioAgent 看 today）
    repo_mock.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 18), symbols=symbols, excluded={}
    )
    result2 = agent.run(AgentContext(state={"today": "2026-06-18"}))
    assert result2["status"] == "ok"
    # 数据没变 → 组合不变 → turnover ≈ 0
    assert result2["turnover"] < 0.1
