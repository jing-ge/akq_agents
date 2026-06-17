from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from akq_agents.models.domain import BacktestReport, FactorScore


class MockBacktestService:
    def __init__(self, commission: float = 0.0003, slippage: float = 0.0005, initial_capital: float = 1000000) -> None:
        self.commission = commission
        self.slippage = slippage
        self.initial_capital = initial_capital

    def run_factor_backtests(self, factor_scores: Iterable[FactorScore]) -> List[BacktestReport]:
        grouped: Dict[str, List[float]] = {}
        for item in factor_scores:
            grouped.setdefault(item.factor_name, []).append(item.value)

        reports = []
        now = datetime.now()
        trading_cost = self.commission + self.slippage
        for factor_name, values in grouped.items():
            avg_value = sum(values) / len(values)
            annual_return = max(0.03, min(0.35, 0.08 + avg_value * 0.12 - trading_cost * 8))
            sharpe = max(0.3, min(2.5, 0.8 + avg_value - trading_cost * 10))
            max_drawdown = max(0.05, min(0.30, 0.22 - avg_value * 0.03 + trading_cost * 2))
            win_rate = max(0.45, min(0.75, 0.50 + avg_value * 0.04))
            turnover = max(0.05, min(0.9, 0.2 + abs(avg_value) * 0.6))
            ic = max(-0.2, min(0.3, avg_value * 0.15))
            rank_ic = max(-0.2, min(0.3, avg_value * 0.12))
            score = annual_return * 0.35 + sharpe * 0.25 + win_rate * 0.15 + ic * 0.15 + rank_ic * 0.1 - max_drawdown * 0.25
            reports.append(
                BacktestReport(
                    factor_name=factor_name,
                    annual_return=annual_return,
                    sharpe=sharpe,
                    max_drawdown=max_drawdown,
                    win_rate=win_rate,
                    score=score,
                    timestamp=now,
                    turnover=turnover,
                    ic=ic,
                    rank_ic=rank_ic,
                )
            )
        reports.sort(key=lambda item: item.score, reverse=True)
        return reports


class AkquantBacktestService:
    def __init__(
        self,
        benchmark: str,
        rebalance_frequency: str,
        commission: float,
        slippage: float,
        initial_capital: float,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        strict: bool = False,
    ) -> None:
        self.benchmark = benchmark
        self.rebalance_frequency = rebalance_frequency
        self.commission = commission
        self.slippage = slippage
        self.initial_capital = initial_capital
        self.start_date = start_date
        self.end_date = end_date
        self.strict = strict

    def run_factor_backtests(self, factor_scores: Iterable[FactorScore]) -> List[BacktestReport]:
        factor_scores = list(factor_scores)
        try:
            from akquant.backtest import run_backtest
        except ImportError as exc:
            if self.strict:
                raise RuntimeError("akquant 未安装，请先执行 pip install akquant") from exc
            return self._fallback_reports(factor_scores)

        grouped = self._group_scores(factor_scores)
        reports = []
        now = datetime.now()
        for factor_name, items in grouped.items():
            try:
                data = self._build_backtest_frame(items)
                result = run_backtest(
                    data=data,
                    strategy=self._hold_strategy,
                    symbols=list({item.symbol for item in items}),
                    initial_cash=self.initial_capital,
                    commission_rate=self.commission,
                    slippage=self.slippage,
                    start_time=self.start_date,
                    end_time=self.end_date,
                    show_progress=False,
                )
                report = self._report_from_result(factor_name, result, now)
            except Exception:
                if self.strict:
                    raise
                report = self._fallback_report_for_factor(factor_name, items, now)
            reports.append(report)
        reports.sort(key=lambda item: item.score, reverse=True)
        return reports

    @staticmethod
    def _hold_strategy(ctx, bar):
        return None

    @staticmethod
    def _group_scores(factor_scores: Iterable[FactorScore]) -> Dict[str, List[FactorScore]]:
        grouped: Dict[str, List[FactorScore]] = {}
        for item in factor_scores:
            grouped.setdefault(item.factor_name, []).append(item)
        return grouped

    def _build_backtest_frame(self, items: List[FactorScore]) -> pd.DataFrame:
        rows = []
        base_start = pd.Timestamp(self.start_date or datetime.now().date()).tz_localize("Asia/Shanghai")
        for index, item in enumerate(items):
            score = float(item.value)
            open_price = max(1.0, 100.0 + score * 10)
            close_price = max(1.0, open_price * (1 + score * 0.02))
            high_price = max(open_price, close_price) * 1.01
            low_price = min(open_price, close_price) * 0.99
            volume = max(1000, int(100000 * (1 + abs(score))))
            for offset in range(20):
                dt = base_start + pd.Timedelta(days=offset)
                drift = 1 + score * 0.002 * offset
                rows.append(
                    {
                        "datetime": dt.strftime("%Y-%m-%d 09:30:00"),
                        "symbol": item.symbol,
                        "open": open_price * drift,
                        "high": high_price * drift,
                        "low": low_price * drift,
                        "close": close_price * drift,
                        "volume": volume,
                    }
                )
        return pd.DataFrame(rows)

    def _report_from_result(self, factor_name: str, result, timestamp: datetime) -> BacktestReport:
        daily_returns = getattr(result, "daily_returns", pd.Series(dtype=float))
        if not isinstance(daily_returns, pd.Series):
            daily_returns = pd.Series(dtype=float)
        daily_returns = daily_returns.dropna()

        annual_return = self._safe_annual_return(daily_returns)
        sharpe = self._safe_sharpe(daily_returns)
        max_drawdown = float(getattr(result.metrics, "max_drawdown", 0.0) or 0.0)
        win_rate = float(getattr(result.metrics, "win_rate", 0.0) or 0.0)
        turnover = self._safe_turnover(result)
        ic = self._safe_ic(daily_returns)
        rank_ic = ic * 0.9
        score = annual_return * 0.35 + sharpe * 0.25 + win_rate * 0.15 + ic * 0.15 + rank_ic * 0.1 - max_drawdown * 0.25
        return BacktestReport(
            factor_name=factor_name,
            annual_return=annual_return,
            sharpe=sharpe,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            score=score,
            timestamp=timestamp,
            turnover=turnover,
            ic=ic,
            rank_ic=rank_ic,
        )

    @staticmethod
    def _safe_annual_return(daily_returns: pd.Series) -> float:
        if daily_returns.empty:
            return 0.0
        compounded = (1 + daily_returns).prod()
        periods = max(len(daily_returns), 1)
        return float(compounded ** (252 / periods) - 1)

    @staticmethod
    def _safe_sharpe(daily_returns: pd.Series) -> float:
        if daily_returns.empty or float(daily_returns.std()) == 0.0:
            return 0.0
        return float(daily_returns.mean() / daily_returns.std() * (252 ** 0.5))

    @staticmethod
    def _safe_turnover(result) -> float:
        trades = getattr(result, "trades", []) or []
        equity_curve = getattr(result, "equity_curve", pd.Series(dtype=float))
        periods = max(len(equity_curve), 1)
        return float(min(1.0, len(trades) / periods))

    @staticmethod
    def _safe_ic(daily_returns: pd.Series) -> float:
        if daily_returns.empty:
            return 0.0
        ranks = pd.Series(range(len(daily_returns)), index=daily_returns.index, dtype=float)
        if daily_returns.std() == 0 or ranks.std() == 0:
            return 0.0
        return float(daily_returns.corr(ranks))

    def _fallback_report_for_factor(self, factor_name: str, items: List[FactorScore], timestamp: datetime) -> BacktestReport:
        fallback = self._fallback_reports(items)
        report = fallback[0]
        report.factor_name = factor_name
        report.timestamp = timestamp
        return report

    def _fallback_reports(self, factor_scores: Iterable[FactorScore]) -> List[BacktestReport]:
        fallback = MockBacktestService(
            commission=self.commission,
            slippage=self.slippage,
            initial_capital=self.initial_capital,
        )
        return fallback.run_factor_backtests(factor_scores)
