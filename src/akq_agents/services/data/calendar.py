"""交易日历内存缓存。

本模块提供一个轻量级 ``TradingCalendar``，负责维护近 N 年交易日集合，并提供
交易日判断、前后交易日查找以及闭区间交易日枚举。

按 P1 设计要求，本模块本身不直接依赖 Repository 或调度逻辑；默认 AKShare 加载仅通过
``default_load_fn_via_akshare`` 工厂函数暴露，便于测试时注入 mock loader。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from datetime import date

from akq_agents.services.data.exceptions import FetchError


class TradingCalendar:
    """交易日历内存缓存。

    :param lookback_years: 仅保留最近多少年的交易日；默认 5 年。
    """

    def __init__(self, lookback_years: int = 5) -> None:
        self.lookback_years = lookback_years
        self._days_set: set[date] = set()
        self._days_sorted: list[date] = []
        self._bootstrapped = False

    def bootstrap(self, load_fn: Callable[[], Iterable[date]]) -> None:
        """加载并替换内存中的交易日集合。

        ``bootstrap`` 可重复调用；相同输入会得到相同结果，因此是幂等的。
        仅保留相对最新交易日起最近 ``lookback_years`` 年内的数据。
        """

        loaded_days = sorted(set(load_fn()))
        if loaded_days:
            latest_day = loaded_days[-1]
            min_year = latest_day.year - self.lookback_years
            loaded_days = [day for day in loaded_days if day.year > min_year]
        self._days_sorted = loaded_days
        self._days_set = set(loaded_days)
        self._bootstrapped = True

    def is_trading_day(self, d: date) -> bool:
        """返回给定日期是否为交易日。"""

        self._ensure_bootstrapped()
        return d in self._days_set

    def previous_trading_day(self, d: date) -> date:
        """返回严格早于 ``d`` 的最近一个交易日。"""

        self._ensure_bootstrapped()
        index = bisect_left(self._days_sorted, d) - 1
        if index < 0:
            raise ValueError(f"No previous trading day before {d}")
        return self._days_sorted[index]

    def next_trading_day(self, d: date) -> date:
        """返回严格晚于 ``d`` 的最近一个交易日。"""

        self._ensure_bootstrapped()
        index = bisect_right(self._days_sorted, d)
        if index >= len(self._days_sorted):
            raise ValueError(f"No next trading day after {d}")
        return self._days_sorted[index]

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """返回 ``[start, end]`` 闭区间内的所有交易日。"""

        self._ensure_bootstrapped()
        if start > end:
            return []
        left = bisect_left(self._days_sorted, start)
        right = bisect_right(self._days_sorted, end)
        return self._days_sorted[left:right]

    def _ensure_bootstrapped(self) -> None:
        if not self._bootstrapped:
            raise RuntimeError("TradingCalendar not bootstrapped")


def default_load_fn_via_akshare() -> Callable[[], list[date]]:
    """返回通过 AKShare 拉取交易日历的默认 loader。"""

    def load() -> list[date]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise FetchError(reason_code="UNKNOWN", message="akshare not installed") from exc

        df = ak.tool_trade_date_hist_sina()
        return [date.fromisoformat(str(value)) for value in df["trade_date"].tolist()]

    return load
