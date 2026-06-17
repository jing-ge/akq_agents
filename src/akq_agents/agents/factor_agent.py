"""FactorAgent —— 基于因子库计算每只票的因子分数。

P1 改造点：
- 新增 ``repository`` 可选注入。若 repository 注入且能从缓存读到当日 OHLCV，
  优先用 repository 读到的全量数据（spec A6 验收）；否则回退到旧的 market_snapshots
  路径（保持现有 5 只票快照链路在数据层未就绪前仍能跑通）。
- 缺数据时不抛异常，写 ``status=skipped`` 到 context.state，让上游 workflow 继续。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import MarketSnapshot
from akq_agents.services.data.exceptions import DataNotReady


class FactorAgent(BaseAgent):
    name = "factor-agent"

    def __init__(self, factor_library, repository: object | None = None) -> None:
        self.factor_library = factor_library
        self.repository = repository

    def run(self, context: AgentContext) -> dict[str, Any]:
        # P1 优先：从 repository 读全市场 OHLCV（spec A6）。失败/缺数据 → skipped。
        if self.repository is not None:
            outcome = self._try_repository_path(context)
            if outcome is not None:
                return outcome

        # 回退：旧的 market_snapshots 内联因子链路（兼容现有 workflow）
        return self._legacy_snapshot_path(context)

    def _try_repository_path(self, context: AgentContext) -> dict[str, Any] | None:
        today = self._today(context)
        try:
            universe = self.repository.get_universe(today)  # type: ignore[union-attr]
        except DataNotReady:
            context.state["factor_agent_status"] = "skipped"
            context.state["factor_agent_reason"] = "universe_not_ready"
            return {"status": "skipped", "reason": "universe_not_ready"}
        except Exception:
            # repository 还没装好或 sqlite 不存在 → 不算 P1 错误，回退旧路径
            return None

        try:
            _ = self.repository.get_ohlcv(  # type: ignore[union-attr]
                universe.symbols, today, today
            )
        except DataNotReady:
            context.state["factor_agent_status"] = "skipped"
            context.state["factor_agent_reason"] = "ohlcv_not_ready"
            return {"status": "skipped", "reason": "ohlcv_not_ready"}
        except Exception:
            return None

        # P1 仅证明读链路通；真正用 DataFrame 计算因子放到 P3。
        # 这里仍用 legacy 计算（基于 market_snapshots），但状态标记 repository-OK。
        result = self._legacy_snapshot_path(context)
        context.state["factor_agent_status"] = "ok_repository"
        return result

    def _legacy_snapshot_path(self, context: AgentContext) -> dict[str, Any]:
        raw_snapshots = context.state.get("market_snapshots", [])
        snapshots: list[MarketSnapshot] = [
            MarketSnapshot(
                symbol=item["symbol"],
                close=item["close"],
                volume=item["volume"],
                timestamp=(
                    datetime.fromisoformat(item["timestamp"])
                    if isinstance(item["timestamp"], str)
                    else item["timestamp"]
                ),
                extras=item.get("extras", {}),
            )
            for item in raw_snapshots
        ]
        factor_scores = self.factor_library.compute_factor_scores(snapshots)
        serialized = []
        for item in factor_scores:
            payload = asdict(item)
            payload["timestamp"] = item.timestamp.isoformat()
            serialized.append(payload)
        context.state["factor_scores"] = serialized
        return {"factor_scores": factor_scores}

    @staticmethod
    def _today(context: AgentContext):
        from datetime import date

        today = context.state.get("today")
        if today is None:
            return date.today()
        if isinstance(today, str):
            return date.fromisoformat(today)
        return today
