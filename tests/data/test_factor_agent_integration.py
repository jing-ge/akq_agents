"""FactorAgent P1 改造的单元测试：repository 注入路径 + 缺数据 skipped 行为（spec A6）。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

from akq_agents.agents.base import AgentContext
from akq_agents.agents.factor_agent import FactorAgent
from akq_agents.services.data.exceptions import DataNotReady
from akq_agents.services.data.schemas import UniverseSnapshot


class _FakeFactorLibrary:
    """最小够用 stub：吃 list[MarketSnapshot]，吐 list[FactorScore]-like。"""

    def __init__(self) -> None:
        self.compute_calls = 0

    def compute_factor_scores(self, snapshots):
        self.compute_calls += 1

        # 返回 dataclass-like 对象列表（asdict 友好）
        from dataclasses import dataclass

        @dataclass
        class _Score:
            symbol: str
            factor_name: str
            value: float
            timestamp: datetime

        return [_Score(symbol=s.symbol, factor_name="dummy", value=1.0, timestamp=s.timestamp) for s in snapshots]


def _base_context_with_snapshots() -> AgentContext:
    snap = {
        "symbol": "600519",
        "close": 1700.0,
        "volume": 100000,
        "timestamp": "2026-06-17T09:30:00",
        "extras": {},
    }
    return AgentContext(state={"market_snapshots": [snap], "today": "2026-06-17"})


def test_legacy_path_when_no_repository() -> None:
    lib = _FakeFactorLibrary()
    agent = FactorAgent(lib, repository=None)
    ctx = _base_context_with_snapshots()
    result = agent.run(ctx)
    assert "factor_scores" in result
    assert lib.compute_calls == 1


def test_repository_universe_not_ready_returns_skipped() -> None:
    lib = _FakeFactorLibrary()
    repo = MagicMock()
    repo.get_universe.side_effect = DataNotReady({"_universe": [date(2026, 6, 17)]})

    agent = FactorAgent(lib, repository=repo)
    ctx = _base_context_with_snapshots()
    result = agent.run(ctx)
    assert result == {"status": "skipped", "reason": "universe_not_ready"}
    assert ctx.state["factor_agent_status"] == "skipped"
    # legacy 路径不应被触发
    assert lib.compute_calls == 0


def test_repository_ohlcv_not_ready_returns_skipped() -> None:
    lib = _FakeFactorLibrary()
    repo = MagicMock()
    repo.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 17),
        symbols=["600519"],
        excluded={},
    )
    repo.get_ohlcv.side_effect = DataNotReady({"600519": [date(2026, 6, 17)]})

    agent = FactorAgent(lib, repository=repo)
    ctx = _base_context_with_snapshots()
    result = agent.run(ctx)
    assert result == {"status": "skipped", "reason": "ohlcv_not_ready"}
    assert ctx.state["factor_agent_status"] == "skipped"


def test_repository_ok_path_falls_through_to_legacy_compute() -> None:
    """P1 阶段 repository OK 时仍跑 legacy 计算（DataFrame->因子计算放 P3）。"""
    lib = _FakeFactorLibrary()
    repo = MagicMock()
    repo.get_universe.return_value = UniverseSnapshot(
        date=date(2026, 6, 17),
        symbols=["600519"],
        excluded={},
    )
    # get_ohlcv 不抛 → 视为 OK
    import pandas as pd

    repo.get_ohlcv.return_value = pd.DataFrame(
        [{"date": date(2026, 6, 17), "symbol": "600519", "close": 1700.0}]
    )

    agent = FactorAgent(lib, repository=repo)
    ctx = _base_context_with_snapshots()
    result = agent.run(ctx)
    assert "factor_scores" in result
    assert ctx.state["factor_agent_status"] == "ok_repository"
    assert lib.compute_calls == 1


def test_repository_unexpected_error_falls_back_to_legacy() -> None:
    """repository 出现非 DataNotReady 异常（如尚未 init）→ 静默回退旧链路。"""
    lib = _FakeFactorLibrary()
    repo = MagicMock()
    repo.get_universe.side_effect = RuntimeError("sqlite missing")

    agent = FactorAgent(lib, repository=repo)
    ctx = _base_context_with_snapshots()
    result = agent.run(ctx)
    assert "factor_scores" in result
    assert lib.compute_calls == 1
    # 没有 status 字段（走的 legacy 分支）
    assert "factor_agent_status" not in ctx.state
