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

import concurrent.futures
import time
import warnings
from datetime import date
from typing import Any, Literal

import pandas as pd

from akq_agents.models.data_config import AkshareGatewayConfig
from akq_agents.services.data.exceptions import FetchError

ReasonCode = Literal["RATE_LIMITED", "SCHEMA_DRIFT", "NETWORK", "UNKNOWN"]

# akshare 底层用 requests 但不暴露 timeout 参数, 半开连接 (对端悄悄断链) 会让单次调用
# 无限挂起, 拖死整条数据拉取链路。这里用一个共享单线程池给每次 _call 加 wall-clock 超时:
# 提交 fn 到线程池, future.result(timeout=...) 到点仍未返回则抛 TimeoutError。
# TimeoutError 已在 _is_retryable 白名单内, 会自然走现有重试逻辑 (不新增分支)。
# 超时的旧线程仍在后台跑 (无法强杀 requests), 但不再阻塞主流程; max_workers 略给宽松些
# 避免多个超时线程堆积把池占满。
_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="akshare-call"
)

# A 股主流指数白名单 (symbol → 新浪源前缀). 白名单专注 A 股宽基指数, 用作 benchmark.
# 注意: 000001 会与个股 '平安银行' 冲突 — 000001 上证指数不放白名单, 个股优先.
_INDEX_PREFIX_MAP: dict[str, str] = {
    "000300": "sh",  # 沪深 300 (上交所)
    "000905": "sh",  # 中证 500
    "000016": "sh",  # 上证 50
    "000852": "sh",  # 中证 1000
    "399006": "sz",  # 创业板指
    "399005": "sz",  # 中小 100
    "899050": "bj",  # 北证 50
}


def _is_index_symbol(symbol: str) -> bool:
    """symbol 是否为已知主流指数. 只识别显式白名单, 避免误判个股."""
    return symbol in _INDEX_PREFIX_MAP


def _with_market_prefix(symbol: str) -> str:
    """把裸 symbol（如 '600519'）转换为新浪格式的带前缀符号（如 'sh600519'）。

    规则（覆盖沪深主板/创业板/科创板/北交所）：
    - 优先匹配指数白名单 (_INDEX_PREFIX_MAP), 000300 → sh000300, 399006 → sz399006
    - 6 开头         → sh
    - 0 / 3 开头     → sz
    - 4 / 8 开头     → bj （北交所，注意新浪源对 bj 支持不全）
    - 其它           → 抛 ValueError
    """
    if not symbol or not symbol[0].isdigit():
        raise ValueError(f"invalid symbol: {symbol!r}")
    # 指数白名单优先 (000300 属沪市, 但 head 是 '0', 不走个股 sz 规则)
    if symbol in _INDEX_PREFIX_MAP:
        return f"{_INDEX_PREFIX_MAP[symbol]}{symbol}"
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
        """单股日线（新浪 ``stock_zh_a_daily``，前复权）；指数走 ``stock_zh_index_daily``.

        返回字段：``date, open, high, low, close, volume, amount``（其它字段如
        ``outstanding_share``/``turnover`` 由新浪源额外提供但本层只取核心列）.

        symbol 判断: 000300/000905/000016/399006 等 A 股主流指数走 index 分支
        (个股 API 拉不到指数, 返回空导致 SCHEMA_DRIFT). 指数无 amount 字段,
        自动用 volume × close 估算填充, 保持列 schema 一致.
        """
        if _is_index_symbol(symbol):
            return self._fetch_index_ohlcv(symbol, start, end)
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

    def _fetch_index_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """A 股指数日线 (新浪 stock_zh_index_daily). 无 amount 字段, 用 volume×close 估算.

        symbol 格式: 000300 (无前缀), 内部按 _with_market_prefix 判定 sh/sz 前缀.
        新浪指数 API 一次返回全历史 (~6000 行), 后续按 [start, end] 过滤.
        """
        ak = self._get_ak_module()
        prefixed = _with_market_prefix(symbol)
        df = self._call(
            "stock_zh_index_daily",
            ak.stock_zh_index_daily,
            symbol=prefixed,
        )
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise FetchError(reason_code="SCHEMA_DRIFT", message=f"missing index cols: {', '.join(missing)}")
        out = df.loc[:, required].copy()
        out["date"] = pd.to_datetime(out["date"])
        # 只保留 [start, end] 区间
        mask = (out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))
        out = out.loc[mask].copy()
        # 补 amount 列 (指数 API 无 amount, 用 volume × close 估算)
        out["amount"] = out["volume"] * out["close"]
        return out.loc[:, ["date", "open", "high", "low", "close", "volume", "amount"]]

    def fetch_market_snapshot_today(self) -> pd.DataFrame:
        """一次性拉今日全市场快照（~13s vs 单股逐拉 ~30min）。

        用 ``stock_zh_a_spot`` 新浪源；返回字段标准化到
        ``[symbol, open, high, low, close, volume, amount]``。

        只能拉**当日**数据，不能拉历史。用于盘后快速增量刷新。
        """
        ak = self._get_ak_module()
        df = self._call("stock_zh_a_spot", ak.stock_zh_a_spot)
        if df is None or df.empty:
            raise FetchError(reason_code="UNKNOWN", message="stock_zh_a_spot 返回空")

        # 新浪 spot 列名：代码 / 名称 / 最新价 / 涨跌额 / 涨跌幅 / 买入 / 卖出 / 昨收 / 今开 / 最高 / 最低 / 成交量 / 成交额 / 时间戳
        # （新浪的 "代码" 列已含 sh/sz 前缀，例如 sh600519；要去前缀）
        col_map = {
            "代码": "symbol_full",
            "今开": "open",
            "最高": "high",
            "最低": "low",
            "最新价": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
        missing = [k for k in col_map if k not in df.columns]
        if missing:
            raise FetchError(
                reason_code="SCHEMA_DRIFT",
                message=f"stock_zh_a_spot missing cols: {', '.join(missing)}",
            )
        out = df.rename(columns=col_map)
        # 去市场前缀 (sh600519 -> 600519)；ak 已经标准 6 位代码
        out["symbol"] = out["symbol_full"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
        out = out.loc[:, ["symbol", "open", "high", "low", "close", "volume", "amount"]].copy()
        # 类型清洗：转 float，无效值 -> NaN -> 直接丢
        for col in ("open", "high", "low", "close", "volume", "amount"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.dropna(subset=["close", "open"])
        out = out[(out["close"] > 0) & (out["volume"] >= 0)]
        return out.reset_index(drop=True)

    def fetch_trading_dates(self) -> list[date]:
        """交易日历（``tool_trade_date_hist_sina``）。"""
        ak = self._get_ak_module()
        df = self._call("tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina)
        if "trade_date" not in df.columns:
            raise FetchError(reason_code="SCHEMA_DRIFT", message="missing cols: trade_date")
        return [pd.Timestamp(value).date() for value in df["trade_date"].tolist()]

    # 同花顺行业板块汇总列名 → 标准化列。东财 _em 系列在本地网络 RemoteDisconnected 不可用
    # (同 fetch_st_list 的处境), 故板块数据源选同花顺 stock_board_industry_summary_ths:
    # 一次调用即给齐看板需要的全部字段 (涨跌幅 / 成交额 / 资金净流入 / 涨跌家数 / 领涨股)。
    _BOARD_THS_RENAME: dict[str, str] = {
        "板块": "board_name",
        "涨跌幅": "pct_chg",
        "总成交额": "amount",
        "净流入": "net_inflow",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
        "领涨股": "leader_name",
        "领涨股-涨跌幅": "leader_pct",
    }
    _BOARD_COLUMNS = [
        "board_name", "pct_chg", "amount", "net_inflow",
        "up_count", "down_count", "leader_name", "leader_pct",
    ]

    def fetch_board_snapshot(self) -> pd.DataFrame:
        """当日行业板块行情快照 (同花顺 ``stock_board_industry_summary_ths``)。

        返回标准化列 ``_BOARD_COLUMNS``:
        ``[board_name, pct_chg, amount, net_inflow, up_count, down_count,
        leader_name, leader_pct]`` —— 一行一个行业板块 (~90 个)。

        - 只给**当日**快照 (接口无历史), 用于盘后落地。
        - 走 ``_call`` 继承限流 / 超时 / 重试 / ``FetchError``。
        - schema 缺列 → ``FetchError(SCHEMA_DRIFT)`` (照抄 ``fetch_ohlcv`` 守卫)。
        """
        ak = self._get_ak_module()
        df = self._call("stock_board_industry_summary_ths", ak.stock_board_industry_summary_ths)
        if df is None or df.empty:
            raise FetchError(reason_code="UNKNOWN", message="stock_board_industry_summary_ths 返回空")

        missing = [src for src in self._BOARD_THS_RENAME if src not in df.columns]
        if missing:
            raise FetchError(
                reason_code="SCHEMA_DRIFT",
                message=f"board summary missing cols: {', '.join(missing)}",
            )
        out = df.rename(columns=self._BOARD_THS_RENAME).loc[:, self._BOARD_COLUMNS].copy()
        # 数值列清洗: 转 float, 无效 -> NaN; 名称列转 str。
        for col in ("pct_chg", "amount", "net_inflow", "up_count", "down_count", "leader_pct"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        for col in ("board_name", "leader_name"):
            out[col] = out[col].astype(str)
        out = out.dropna(subset=["board_name", "pct_chg"]).reset_index(drop=True)
        return out

    def fetch_board_hist(self, board_name: str, start: date, end: date) -> pd.DataFrame:
        """单个行业板块的历史日线（同花顺 ``stock_board_industry_index_ths``）。

        入参是**板块名**（与 ``fetch_board_snapshot`` 的 ``board_name`` 同一命名）。
        接口只给指数点位（无涨跌幅），故用收盘价 ``pct_change`` 自算 ``pct_chg``。

        返回标准化列 ``[date, board_name, pct_chg]`` —— 一行一个交易日。
        - ``pct_chg`` 单位 %，首日无前值 → 该行被丢弃（调用方应把窗口往前多留一天）。
        - 走 ``_call`` 继承限流 / 超时 / 重试 / ``FetchError``。
        - schema 缺列 → ``FetchError(SCHEMA_DRIFT)``。
        """
        ak = self._get_ak_module()
        df = self._call(
            "stock_board_industry_index_ths",
            ak.stock_board_industry_index_ths,
            symbol=board_name,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "board_name", "pct_chg"])
        if "日期" not in df.columns or "收盘价" not in df.columns:
            raise FetchError(
                reason_code="SCHEMA_DRIFT",
                message=f"board hist missing cols for {board_name}: got {list(df.columns)}",
            )
        out = df.rename(columns={"日期": "date", "收盘价": "close"}).loc[:, ["date", "close"]].copy()
        out["date"] = pd.to_datetime(out["date"]).dt.date
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        out["pct_chg"] = (out["close"].pct_change() * 100).round(3)
        out["board_name"] = str(board_name)
        return out.dropna(subset=["pct_chg"]).loc[:, ["date", "board_name", "pct_chg"]].reset_index(drop=True)

    def fetch_board_kline(self, board_name: str, start: date, end: date) -> pd.DataFrame:
        """单个行业板块的 OHLC 日 K（同花顺 ``stock_board_industry_index_ths``）。

        与 ``fetch_board_hist`` 同一数据源，但保留完整 OHLC + 成交量，供 K 线图。
        返回标准化列 ``[date, open, high, low, close, volume]``（按日期升序）。
        走 ``_call`` 继承限流 / 超时 / 重试 / ``FetchError``；schema 缺列 → SCHEMA_DRIFT。
        """
        ak = self._get_ak_module()
        df = self._call(
            "stock_board_industry_index_ths",
            ak.stock_board_industry_index_ths,
            symbol=board_name,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        rename = {"日期": "date", "开盘价": "open", "最高价": "high", "最低价": "low", "收盘价": "close", "成交量": "volume"}
        missing = [src for src in rename if src not in df.columns]
        if missing:
            raise FetchError(
                reason_code="SCHEMA_DRIFT",
                message=f"board kline missing cols for {board_name}: {missing}",
            )
        out = df.rename(columns=rename).loc[:, ["date", "open", "high", "low", "close", "volume"]].copy()
        out["date"] = pd.to_datetime(out["date"]).dt.date
        for col in ("open", "high", "low", "close", "volume"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out.dropna(subset=["open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)

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
                return self._call_with_timeout(fn, *args, **kwargs)
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

    def _call_with_timeout(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """给单次 akshare 调用加 wall-clock 超时保护。

        akshare 不暴露 timeout, 半开连接会无限挂起。把 fn 提交到共享线程池,
        future.result(timeout=timeout_s) 到点未返回抛 TimeoutError (可重试)。
        timeout_s <= 0 视为禁用超时, 直接同步调用 (保底兼容, 不改变旧行为)。
        """
        timeout_s = getattr(self._config, "timeout_s", 0)
        if not timeout_s or timeout_s <= 0:
            return fn(*args, **kwargs)
        future = _CALL_EXECUTOR.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            # 转成内置 TimeoutError 让 _is_retryable / _retry_exhausted_reason 识别为 NETWORK 类可重试。
            future.cancel()  # 尚未开始执行的能取消; 已在跑的挂起线程只能任其后台结束。
            raise TimeoutError(f"akshare call timed out after {timeout_s}s") from exc

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
