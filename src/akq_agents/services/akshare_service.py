from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

import pandas as pd

from akq_agents.models.domain import MarketSnapshot


class MockAkshareService:
    def fetch_market_snapshots(self, symbols: List[str], lookback_days: int = 120) -> List[MarketSnapshot]:
        now = datetime.now()
        snapshots = []
        for index, symbol in enumerate(symbols, start=1):
            close = 10.0 + index * 7.3
            volume = 1_000_000 + index * 250_000
            extras = {
                "momentum_5": 0.01 * index,
                "momentum_20": 0.02 * index,
                "momentum_60": 0.03 * index,
                "reversal_5": -0.005 * index,
                "volatility_20": 0.01 * (6 - index),
                "turnover_ratio": 0.03 * index,
                "value_score": 0.15 * index,
                "quality_score": 0.12 * (6 - index),
                "size_score": -0.08 * index,
            }
            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    close=close,
                    volume=volume,
                    timestamp=now,
                    extras=extras,
                )
            )
        return snapshots


class AkshareService:
    def fetch_market_snapshots(self, symbols: List[str], lookback_days: int = 120) -> List[MarketSnapshot]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("akshare 未安装，请先执行 pip install akshare") from exc

        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_days)
        snapshots = []
        for symbol in symbols:
            data = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if data.empty:
                continue
            data = self._normalize_hist_columns(data)
            latest = data.iloc[-1]
            close = float(latest["close"])
            volume = float(latest["volume"])
            amount = float(latest.get("amount", 0.0))
            high = float(latest.get("high", close))
            low = float(latest.get("low", close))
            turnover_ratio = self._safe_turnover_ratio(amount, close, volume)
            amplitude = 0.0 if close == 0 else (high - low) / close
            extras: Dict[str, float] = {
                "momentum_5": self._safe_return(data, 5),
                "momentum_20": self._safe_return(data, 20),
                "momentum_60": self._safe_return(data, 60),
                "reversal_5": -self._safe_return(data, 5),
                "volatility_20": self._safe_volatility(data, 20),
                "turnover_ratio": turnover_ratio,
                "value_score": self._estimate_value_score(close, amount, turnover_ratio),
                "quality_score": self._estimate_quality_score(data),
                "size_score": self._estimate_size_score(amount),
                "amplitude_20": self._safe_amplitude(data, 20),
                "close_to_high": self._safe_close_to_high(close, high),
                "volume_trend": self._safe_volume_trend(data, 20),
                "intraday_range": amplitude,
            }
            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    close=close,
                    volume=volume,
                    timestamp=datetime.now(),
                    extras=extras,
                )
            )
        return snapshots

    @staticmethod
    def _normalize_hist_columns(data: pd.DataFrame) -> pd.DataFrame:
        mapping = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_change",
        }
        renamed = data.rename(columns={key: value for key, value in mapping.items() if key in data.columns}).copy()
        return renamed

    @staticmethod
    def _safe_return(data: pd.DataFrame, window: int) -> float:
        if len(data) <= window:
            return 0.0
        current = float(data.iloc[-1]["close"])
        past = float(data.iloc[-window - 1]["close"])
        if past == 0:
            return 0.0
        return current / past - 1.0

    @staticmethod
    def _safe_volatility(data: pd.DataFrame, window: int) -> float:
        if len(data) <= window:
            return 0.0
        returns = data["close"].pct_change().dropna().tail(window)
        if returns.empty:
            return 0.0
        return float(returns.std())

    @staticmethod
    def _safe_turnover_ratio(amount: float, close: float, volume: float) -> float:
        base = max(close * volume, 1.0)
        return float(amount / base) if amount > 0 else 0.0

    @staticmethod
    def _estimate_value_score(close: float, amount: float, turnover_ratio: float) -> float:
        if close <= 0:
            return 0.0
        liquidity_penalty = min(1.0, turnover_ratio)
        raw = (amount / max(close, 1.0)) / 1e8 - 0.5 - liquidity_penalty * 0.2
        return max(-1.0, min(1.0, raw))

    @staticmethod
    def _estimate_quality_score(data: pd.DataFrame) -> float:
        if len(data) < 20:
            return 0.0
        rolling_mean = data["close"].tail(20).mean()
        rolling_std = data["close"].pct_change().tail(20).std()
        latest = float(data.iloc[-1]["close"])
        if rolling_mean == 0:
            return 0.0
        trend = latest / rolling_mean - 1.0
        stability = 1.0 - min(1.0, float(rolling_std) * 10) if pd.notna(rolling_std) else 0.0
        return max(-1.0, min(1.0, trend * 0.7 + stability * 0.3))

    @staticmethod
    def _estimate_size_score(amount: float) -> float:
        if amount <= 0:
            return 0.0
        return max(-1.0, min(1.0, -amount / 1e9))

    @staticmethod
    def _safe_amplitude(data: pd.DataFrame, window: int) -> float:
        if len(data) < window or "high" not in data.columns or "low" not in data.columns or "close" not in data.columns:
            return 0.0
        subset = data.tail(window)
        avg_close = subset["close"].mean()
        if avg_close == 0:
            return 0.0
        return float((subset["high"] - subset["low"]).mean() / avg_close)

    @staticmethod
    def _safe_close_to_high(close: float, high: float) -> float:
        if high == 0:
            return 0.0
        return float(close / high - 1.0)

    @staticmethod
    def _safe_volume_trend(data: pd.DataFrame, window: int) -> float:
        if len(data) < window or "volume" not in data.columns:
            return 0.0
        subset = data.tail(window)
        start = float(subset.iloc[0]["volume"])
        end = float(subset.iloc[-1]["volume"])
        if start == 0:
            return 0.0
        return float(end / start - 1.0)
