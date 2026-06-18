"""DataAgent —— 收集市场快照。

变更：若 services 注入了 ``data_repository``（P1 缓存），优先从缓存读取最近一日
全市场 OHLCV，构造 ``MarketSnapshot``。这样：

1. 无网环境也能跑通 ``run-once``（关键：解锁后续因子/回测/组合链路）；
2. 不再受 ``universe.symbols`` 几只票限制，自然覆盖全 A。

未注入 repository 或缓存读不到 → 回退旧的 market_service 直连路径。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import MarketSnapshot


class DataAgent(BaseAgent):
    name = "data-agent"

    def __init__(
        self,
        market_service,
        symbols,
        lookback_days: int,
        repository: object | None = None,
    ):
        self.market_service = market_service
        self.symbols = symbols
        self.lookback_days = lookback_days
        self.repository = repository

    def run(self, context: AgentContext):
        # P1 优先：从缓存读，构造 MarketSnapshot 列表（含 momentum/vol/turnover extras）
        if self.repository is not None:
            try:
                snapshots = self._from_repository()
                if snapshots:
                    self._write_state(context, snapshots)
                    context.state["data_agent_status"] = "ok_repository"
                    return {"snapshots": snapshots, "source": "repository"}
            except Exception as exc:  # noqa: BLE001 缓存路径失败 → 回退而不是炸
                context.state["data_agent_repository_error"] = str(exc)

        # 回退：旧链路（直连 AKShare 或 mock）
        snapshots = self.market_service.fetch_market_snapshots(self.symbols, self.lookback_days)
        self._write_state(context, snapshots)
        context.state["data_agent_status"] = "ok_market_service"
        return {"snapshots": snapshots, "source": "market_service"}

    # ---- helpers ----------------------------------------------------------

    def _from_repository(self) -> list[MarketSnapshot]:
        repo = self.repository
        latest = self._latest_cached_date(repo)
        if latest is None:
            return []
        start = latest - timedelta(days=self.lookback_days + 30)  # 多留缓冲，覆盖非交易日
        try:
            universe = repo.get_universe(latest)  # type: ignore[union-attr]
        except Exception:
            return []
        symbols = universe.symbols
        if not symbols:
            return []
        # repository.get_ohlcv 在任一天缺数据会抛 DataNotReady；缓冲缺一两天属正常
        # 这里改成直接读 parquet 区间，宽容缺失
        frame = self._read_ohlcv_loose(repo, symbols, start, latest)
        if frame.empty:
            return []
        return self._build_snapshots(frame, latest)

    @staticmethod
    def _latest_cached_date(repo) -> date | None:
        ohlcv_root = getattr(repo, "_ohlcv_dir", None)
        if ohlcv_root is None or not ohlcv_root.exists():
            return None
        dates: list[date] = []
        for p in ohlcv_root.glob("date=*"):
            try:
                dates.append(date.fromisoformat(p.name.split("=", 1)[1]))
            except Exception:
                continue
        if not dates:
            return None
        return max(dates)

    @staticmethod
    def _read_ohlcv_loose(repo, symbols, start: date, end: date):
        """直接通过 pyarrow 读取区间数据；不要求每天都齐全（避免 DataNotReady）。"""
        import pyarrow.dataset as ds
        import pandas as pd

        dataset = ds.dataset(repo._ohlcv_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(list(symbols)),
        )
        frame = table.to_pandas()
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    @staticmethod
    def _build_snapshots(frame, latest: date) -> list[MarketSnapshot]:
        """为每只票构造一条 MarketSnapshot，extras 内置常用衍生因子。"""
        import pandas as pd

        ts = datetime.combine(latest, datetime.min.time())
        out: list[MarketSnapshot] = []
        for symbol, sub in frame.groupby("symbol", sort=False):
            sub = sub.sort_values("date")
            if sub.empty:
                continue
            last = sub.iloc[-1]
            close = float(last["close"])
            volume = float(last.get("volume", 0.0) or 0.0)
            amount = float(last.get("amount", 0.0) or 0.0)
            extras = {
                "momentum_5": _ret(sub, 5),
                "momentum_20": _ret(sub, 20),
                "momentum_60": _ret(sub, 60),
                "reversal_5": -_ret(sub, 5),
                "volatility_20": _vol(sub, 20),
                "turnover_ratio": (amount / max(close * volume, 1.0)) if amount > 0 else 0.0,
                "amount_20": _amount_mean(sub, 20),
            }
            out.append(
                MarketSnapshot(
                    symbol=str(symbol),
                    close=close,
                    volume=volume,
                    timestamp=ts,
                    extras=extras,
                )
            )
        return out

    @staticmethod
    def _write_state(context: AgentContext, snapshots: list[MarketSnapshot]) -> None:
        serialized = []
        for item in snapshots:
            payload = asdict(item)
            payload["timestamp"] = item.timestamp.isoformat()
            serialized.append(payload)
        context.state["market_snapshots"] = serialized


def _ret(sub, window: int) -> float:
    if len(sub) <= window:
        return 0.0
    cur = float(sub.iloc[-1]["close"])
    past = float(sub.iloc[-window - 1]["close"])
    if past == 0:
        return 0.0
    return cur / past - 1.0


def _vol(sub, window: int) -> float:
    if len(sub) <= window:
        return 0.0
    rets = sub["close"].pct_change().dropna().tail(window)
    if rets.empty:
        return 0.0
    return float(rets.std())


def _amount_mean(sub, window: int) -> float:
    if "amount" not in sub.columns or len(sub) == 0:
        return 0.0
    return float(sub["amount"].tail(window).mean() or 0.0)
