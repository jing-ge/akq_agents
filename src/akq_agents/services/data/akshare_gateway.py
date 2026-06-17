"""AKShare 数据访问单一出口。"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Literal

import pandas as pd

from akq_agents.models.data_config import AkshareGatewayConfig
from akq_agents.services.data.exceptions import FetchError

ReasonCode = Literal["RATE_LIMITED", "SCHEMA_DRIFT", "NETWORK", "UNKNOWN"]


class AKShareGateway:
    """封装 AKShare 调用，提供限频、重试和字段标准化。"""

    _HIST_RENAME = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    _SPOT_RENAME = {
        "代码": "symbol",
        "名称": "name",
        "最新价": "price",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover_ratio",
    }

    def __init__(self, config: AkshareGatewayConfig, ak_module: Any | None = None) -> None:
        self._config = config
        self._ak_module = ak_module
        self._last_call_ts: float | None = None

    def fetch_spot(self) -> pd.DataFrame:
        """全市场行情快照（ak.stock_zh_a_spot_em）。
        返回标准化字段：symbol, name, price, volume, amount, turnover_ratio。
        """
        ak = self._get_ak_module()
        df = self._call("stock_zh_a_spot_em", ak.stock_zh_a_spot_em)
        return self._rename_and_validate(df, self._SPOT_RENAME)

    def fetch_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """单股日线（ak.stock_zh_a_hist, period=daily, adjust=qfq）。
        返回标准化字段：date, open, high, low, close, volume, amount。
        """
        ak = self._get_ak_module()
        df = self._call(
            "stock_zh_a_hist",
            ak.stock_zh_a_hist,
            symbol=symbol,
            period="daily",
            adjust="qfq",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        renamed = self._rename_and_validate(df, self._HIST_RENAME)
        renamed["date"] = pd.to_datetime(renamed["date"])
        return renamed[["date", "open", "high", "low", "close", "volume", "amount"]]

    def fetch_trading_dates(self) -> list[date]:
        """ak.tool_trade_date_hist_sina；返回 date 列表。"""
        ak = self._get_ak_module()
        df = self._call("tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina)
        if "trade_date" not in df.columns:
            raise FetchError(reason_code="SCHEMA_DRIFT", message="missing cols: trade_date")
        return [pd.Timestamp(value).date() for value in df["trade_date"].tolist()]

    def fetch_st_list(self) -> list[str]:
        """ak.stock_zh_a_st_em；返回 ST 标的 symbol 列表（不含市场前缀）。"""
        ak = self._get_ak_module()
        df = self._call("stock_zh_a_st_em", ak.stock_zh_a_st_em)
        if "代码" not in df.columns:
            raise FetchError(reason_code="SCHEMA_DRIFT", message="missing cols: 代码")
        return df["代码"].astype(str).tolist()

    def fetch_individual_info(self, symbol: str) -> dict[str, date | bool | None]:
        """ak.stock_individual_info_em(symbol)；返回标准字段字典。"""
        ak = self._get_ak_module()
        df = self._call("stock_individual_info_em", ak.stock_individual_info_em, symbol=symbol)
        if not {"item", "value"}.issubset(df.columns):
            raise FetchError(reason_code="SCHEMA_DRIFT", message="missing cols: item, value")
        mapping = dict(zip(df["item"], df["value"], strict=False))
        listing_value = mapping.get("上市时间")
        suspended_value = mapping.get("停牌")
        listing_date = pd.Timestamp(listing_value).date() if listing_value else None
        is_suspended = None if suspended_value is None else str(suspended_value).strip() in {"是", "true", "True", "1"}
        return {"listing_date": listing_date, "is_suspended": is_suspended}

    def _get_ak_module(self) -> Any:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak
        except ImportError as exc:
            raise FetchError(reason_code="UNKNOWN", message="akshare not installed") from exc
        self._ak_module = ak
        return ak

    def _throttle(self) -> None:
        min_interval = 1 / self._config.qps
        now = time.monotonic()
        if self._last_call_ts is not None:
            elapsed = now - self._last_call_ts
            if elapsed < min_interval:
                sleep_for = min_interval - elapsed
                time.sleep(sleep_for)
                now = now + sleep_for
        self._last_call_ts = now

    def _call(self, label: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if not self._is_retryable(exc):
                    raise FetchError(reason_code="UNKNOWN", message=str(exc)) from exc
                if attempt == self._config.max_retries:
                    reason_code: ReasonCode = self._retry_exhausted_reason(exc)
                    raise FetchError(
                        reason_code=reason_code,
                        message=str(exc),
                    ) from exc
                time.sleep(self._config.backoff_base_s * (2**attempt))
        raise FetchError(reason_code="UNKNOWN", message=f"unexpected failure in {label}: {last_error}")

    def _rename_and_validate(self, df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
        missing = [source for source in mapping if source not in df.columns]
        if missing:
            raise FetchError(reason_code="SCHEMA_DRIFT", message=f"missing cols: {', '.join(missing)}")
        renamed = df.rename(columns=mapping)
        return renamed[list(mapping.values())]

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return True
        message = str(exc).lower()
        return any(token in message for token in ("timeout", "connection", "429", "rate limit"))

    def _retry_exhausted_reason(self, exc: Exception) -> ReasonCode:
        message = str(exc).lower()
        if "429" in message or "rate limit" in message:
            return "RATE_LIMITED"
        return "NETWORK"
