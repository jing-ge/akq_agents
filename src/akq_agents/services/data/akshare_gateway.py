"""AKShare 数据访问单一出口。

P1.5 重构：东方财富 ``_em`` 系列在部分本地网络下完全不可用（持续
``RemoteDisconnected``）。本网关切到稳定的新浪 + 交易所官方源：

- ``fetch_spot``：合并 ``stock_info_sh_name_code`` + ``stock_info_sz_name_code``
  得到全 A 股（沪+深）的 ``[symbol, name, listing_date]``。**不再返回 price**。
- ``fetch_ohlcv``：``stock_zh_a_daily`` (新浪) + 自动加市场前缀。单股一次可拉
  多年，0.6 秒/只。
- ``fetch_st_list`` / ``fetch_individual_info``：东财源仍不可用 → 退化为空
  stub + warning，让上层过滤器优雅降级。

注意：``fetch_spot`` 返回的列与历史 P1 设计 ``[symbol, name, price, volume,
amount, turnover_ratio]`` 不一致 —— 上层 ``UniverseManager`` 已同步调整以
适配新列。
"""

from __future__ import annotations

import time
import warnings
from datetime import date
from typing import Any, Literal

import pandas as pd

from akq_agents.models.data_config import AkshareGatewayConfig
from akq_agents.services.data.exceptions import FetchError

ReasonCode = Literal["RATE_LIMITED", "SCHEMA_DRIFT", "NETWORK", "UNKNOWN"]


def _with_market_prefix(symbol: str) -> str:
    """把裸 symbol（如 '600519'）转换为新浪格式的带前缀符号（如 'sh600519'）。

    规则（覆盖沪深主板/创业板/科创板/北交所）：
    - 6 开头         → sh
    - 0 / 3 开头     → sz
    - 4 / 8 开头     → bj （北交所，注意新浪源对 bj 支持不全）
    - 其它           → 抛 ValueError
    """
    if not symbol or not symbol[0].isdigit():
        raise ValueError(f"invalid symbol: {symbol!r}")
    head = symbol[0]
    if head == "6":
        return f"sh{symbol}"
    if head in ("0", "3"):
        return f"sz{symbol}"
    if head in ("4", "8"):
        return f"bj{symbol}"
    raise ValueError(f"unsupported market prefix for symbol: {symbol!r}")


class AKShareGateway:
    """封装 AKShare 调用，提供限频、重试和字段标准化。"""

    # 沪深两交易所列表字段映射 → 统一为 [symbol, name, listing_date]
    _SH_RENAME = {"证券代码": "symbol", "证券简称": "name", "上市日期": "listing_date"}
    _SZ_RENAME = {"A股代码": "symbol", "A股简称": "name", "A股上市日期": "listing_date"}

    def __init__(self, config: AkshareGatewayConfig, ak_module: Any | None = None) -> None:
        self._config = config
        self._ak_module = ak_module
        self._last_call_ts: float | None = None

    # ------------------------------------------------------------------ public

    def fetch_spot(self) -> pd.DataFrame:
        """合并沪深交易所官方列表，返回 ``[symbol, name, listing_date]``。

        - 上海所：``stock_info_sh_name_code``
        - 深圳所：``stock_info_sz_name_code``
        - 不返回 price（与 P1 spec 原设计不同；新浪源没有便宜的全市场快照）
        """
        ak = self._get_ak_module()
        sh_df = self._call("stock_info_sh_name_code", ak.stock_info_sh_name_code)
        sz_df = self._call("stock_info_sz_name_code", ak.stock_info_sz_name_code)

        sh_part = self._normalize_listing(sh_df, self._SH_RENAME, source="sh")
        sz_part = self._normalize_listing(sz_df, self._SZ_RENAME, source="sz")
        combined = pd.concat([sh_part, sz_part], ignore_index=True)
        combined = combined.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
        return combined

    def fetch_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """单股日线（新浪 ``stock_zh_a_daily``，前复权）。

        返回字段：``date, open, high, low, close, volume, amount``（其它字段如
        ``outstanding_share``/``turnover`` 由新浪源额外提供但本层只取核心列）。
        """
        ak = self._get_ak_module()
        prefixed = _with_market_prefix(symbol)
        df = self._call(
            "stock_zh_a_daily",
            ak.stock_zh_a_daily,
            symbol=prefixed,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
        required = ["date", "open", "high", "low", "close", "volume", "amount"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise FetchError(reason_code="SCHEMA_DRIFT", message=f"missing cols: {', '.join(missing)}")
        out = df.loc[:, required].copy()
        out["date"] = pd.to_datetime(out["date"])
        return out

    def fetch_trading_dates(self) -> list[date]:
        """交易日历（``tool_trade_date_hist_sina``）。"""
        ak = self._get_ak_module()
        df = self._call("tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina)
        if "trade_date" not in df.columns:
            raise FetchError(reason_code="SCHEMA_DRIFT", message="missing cols: trade_date")
        return [pd.Timestamp(value).date() for value in df["trade_date"].tolist()]

    def fetch_st_list(self) -> list[str]:
        """ST 列表退化为空 stub。

        东方财富 ``stock_zh_a_st_em`` 在本地网络持续 ``RemoteDisconnected``。
        ST 过滤暂时关闭，后续若找到稳定的 ST 数据源再恢复。
        """
        warnings.warn(
            "fetch_st_list: ST source unavailable on current network; returning empty list. "
            "ST filtering is disabled until a stable source is integrated.",
            RuntimeWarning,
            stacklevel=2,
        )
        return []

    def fetch_individual_info(self, symbol: str) -> dict[str, date | bool | None]:
        """个股信息退化为 stub。

        东方财富 ``stock_individual_info_em`` 在本地网络持续失败。``listing_date``
        现在统一从 ``fetch_spot`` 的交易所列表里获取（已在 UniverseManager 调整），
        本方法仅作为旧接口的兼容回退，返回全 None。
        """
        return {"listing_date": None, "is_suspended": None}

    # ------------------------------------------------------------------ helpers

    def _normalize_listing(
        self, df: pd.DataFrame, mapping: dict[str, str], source: str
    ) -> pd.DataFrame:
        missing = [src for src in mapping if src not in df.columns]
        if missing:
            raise FetchError(
                reason_code="SCHEMA_DRIFT",
                message=f"{source} listing missing cols: {', '.join(missing)}",
            )
        renamed = df.rename(columns=mapping)[list(mapping.values())].copy()
        renamed["symbol"] = renamed["symbol"].astype(str).str.strip()
        renamed["name"] = renamed["name"].astype(str).str.strip()
        # listing_date 在交易所列表里可能是 'YYYY-MM-DD' 字符串或 pd.Timestamp
        renamed["listing_date"] = pd.to_datetime(renamed["listing_date"], errors="coerce").dt.date
        return renamed

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
