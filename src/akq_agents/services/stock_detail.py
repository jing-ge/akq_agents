"""个股详情聚合服务。

给 ``/stock/{symbol}`` 页面提供三类数据的统一入口：

1. **overview**：hero 卡数据。symbol / name / 行业 / 市值 / PE-PB / 今日快照
   （开高低收 + 涨跌幅 + 成交量额 + 换手率） + 至今涨幅。
2. **kline**：K 线数据。D / W / M / Y 走本地 parquet + 后端 resample；
   分钟级（1/5/15/30/60/120m）实时拉 akshare。
3. **intraday**：分时 / 五日分时。

设计原则：

- **本地 parquet 为主，akshare 兜底**：冷路径（K 线历史）优先本地；热路径
  （今日快照 / 估值 / 行业）用 akshare 5min TTL 缓存。
- **显式降级**：任何 akshare 接口失败都不阻断页面；overview 返回
  ``degraded_fields`` 列表告诉前端哪些字段是 "-"。
- **单进程内内存缓存**：spot 全 A 快照 5min TTL，避免多个 overview 调用
  各拉一次 akshare。
- **不写库**：本 module 纯读，不修改任何持久化状态。

**注意**：不走 ``DataExplorerService``——那里为通用浏览器场景优化，会把 5000+
行 spot 结果 head(200) 截断；本服务需要按 symbol 精确查找完整结果。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 全 A 今日快照缓存：单进程内 5 分钟 TTL
_SPOT_CACHE_TTL_S = 300
_spot_cache: dict[str, Any] = {"ts": 0.0, "df": None}

# 个股信息缓存：单进程内 30 分钟 TTL（行业/市值这些字段不会盘中频繁变化）
_INFO_CACHE_TTL_S = 1800
_info_cache: dict[str, dict[str, Any]] = {}  # symbol -> {"ts", "data"}

# 估值指标缓存：15 分钟 TTL
_INDICATOR_CACHE_TTL_S = 900
_indicator_cache: dict[str, dict[str, Any]] = {}  # symbol -> {"ts", "data"}

# 申万一级行业列表缓存：1 小时 TTL
_SW_CACHE_TTL_S = 3600
_sw_cache: dict[str, Any] = {"ts": 0.0, "df": None}


def _ak_call_with_retry(fn, *args, attempts: int = 3, base_delay: float = 0.3, label: str = "akshare", **kwargs):
    """akshare em 域名在本地网络容易 RemoteDisconnected. 短重试 3 次让毛刺自愈.

    延时 0.3s → 0.6s → 0.9s. 3 次都挂就 re-raise 最后一次异常, 调用方各自 catch.
    label 只用于 log; 不影响行为.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                logger.debug("%s attempt %d failed (%s), retrying...", label, attempt + 1, exc)
                time.sleep(base_delay * (attempt + 1))
    assert last_exc is not None
    raise last_exc


@dataclass
class StockOverview:
    """hero 卡返回结构。"""

    symbol: str
    name: str | None
    industry: str | None
    industry_pct_change: float | None
    market_cap: float | None
    pe_ratio: float | None
    pb_ratio: float | None
    listing_date: str | None
    quote: dict[str, Any]  # price / pct_change / open / prev_close / high / low / volume / amount / turnover_ratio / since_listing_pct
    as_of: str
    degraded_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "industry": self.industry,
            "industry_pct_change": self.industry_pct_change,
            "market_cap": self.market_cap,
            "pe_ratio": self.pe_ratio,
            "pb_ratio": self.pb_ratio,
            "listing_date": self.listing_date,
            "quote": self.quote,
            "as_of": self.as_of,
            "degraded_fields": self.degraded_fields,
        }


class StockDetailService:
    """个股详情服务。所有方法都是幂等只读的。"""

    def __init__(self, repo: Any, name_store: Any = None, ak_module: Any = None) -> None:
        """
        :param repo: :class:`DataRepository`，读本地 parquet
        :param name_store: :class:`StockNameStore`，可选；未传时 name 字段为 None
        :param ak_module: 允许测试注入 mock akshare（默认 lazy import 真实模块）
        """
        self._repo = repo
        self._name_store = name_store
        self._ak = ak_module
        self._names_cache: dict[str, str] | None = None

    # ---------------------------------------------------------------- overview

    def fetch_overview_quick(self, symbol: str) -> StockOverview:
        """快速 overview: 只读本地数据源, 不碰任何 akshare 网络接口.

        用途: 前端首屏立即渲染 hero 卡骨架 (name / 上市日期 / 至今涨幅估算).
        目标响应 <100ms. 完整字段 (今日快照 / 行业 / 市值 / PE-PB) 由前端
        并行调 :meth:`fetch_overview` 补齐. degraded_fields 会列出所有
        走 akshare 的字段, 告知前端"这些字段等 full 回来才有".

        至今涨幅 (since_listing_pct) 用本地 parquet 首末收盘估算 —
        非实时但足够展示; full 回来后会用真实现价刷新.
        """
        degraded: list[str] = [
            "quote", "industry", "market_cap", "industry_pct_change",
            "pe_ratio", "pb_ratio",
        ]

        name = self._lookup_name(symbol)
        if name is None:
            degraded.append("name")

        quote: dict[str, Any] = {}
        # 至今涨幅: parquet 首末收盘估算 (无需实时价). 首屏可先亮出.
        try:
            since_pct = self._compute_since_listing_pct_from_local(symbol)
            if since_pct is not None:
                quote["since_listing_pct"] = since_pct
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_detail.quick since_pct failed for %s: %s", symbol, exc)

        # 上市日期: 本地 parquet 首个交易日近似 (真实上市日在 individual_info)
        try:
            listing_local = self._first_bar_date_from_local(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_detail.quick first_bar failed for %s: %s", symbol, exc)
            listing_local = None

        return StockOverview(
            symbol=symbol,
            name=name,
            industry=None,
            industry_pct_change=None,
            market_cap=None,
            pe_ratio=None,
            pb_ratio=None,
            listing_date=listing_local,  # 近似, full 回来会覆盖为真实值
            quote=quote,
            as_of=datetime.now().isoformat(timespec="seconds"),
            degraded_fields=degraded,
        )

    def fetch_overview(self, symbol: str) -> StockOverview:
        """构造 hero 卡数据。所有 akshare 依赖失败都降级为 None + degraded_fields。"""
        degraded: list[str] = []
        # 1) 名称
        name = self._lookup_name(symbol)
        if name is None:
            degraded.append("name")

        # 2) 今日快照（开高低收 / 涨跌幅 / 成交量额 / 换手率）
        quote: dict[str, Any] = {}
        try:
            snapshot_df = self._spot_snapshot()
            if snapshot_df is None:
                # snapshot 拉取失败（akshare 崩溃或返回 None），quote 降级
                degraded.append("quote")
            else:
                quote = self._pick_spot_row(snapshot_df, symbol)
                # snapshot 拿到了但里面没有该 symbol 不算 quote 降级
                # （视为该 symbol 未上市 / 已退市 / 未在 spot 覆盖内）
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_detail.fetch_overview spot failed for %s: %s", symbol, exc)
            if "quote" not in degraded:
                degraded.append("quote")

        # 3) 至今涨幅（本地 parquet 最早交易日的收盘 → 当前价）
        since_pct = None
        try:
            since_pct = self._compute_since_listing_pct(symbol, quote.get("price"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_detail.since_listing_pct failed for %s: %s", symbol, exc)
        if since_pct is not None:
            quote["since_listing_pct"] = since_pct

        # 4) 行业 / 市值
        industry = None
        market_cap = None
        listing_date = None
        try:
            info = self._fetch_individual_info(symbol)
            industry = info.get("industry")
            market_cap = info.get("market_cap")
            listing_date = info.get("listing_date")
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_detail.individual_info failed for %s: %s", symbol, exc)
        if industry is None:
            degraded.append("industry")
        if market_cap is None:
            degraded.append("market_cap")

        # 5) 行业涨跌幅
        industry_pct = None
        if industry is not None:
            try:
                industry_pct = self._fetch_industry_pct(industry)
            except Exception as exc:  # noqa: BLE001
                logger.debug("stock_detail.industry_pct failed for %s: %s", symbol, exc)
        if industry_pct is None:
            degraded.append("industry_pct_change")

        # 6) PE / PB
        pe = pb = None
        try:
            ind = self._fetch_indicator(symbol)
            pe = ind.get("pe")
            pb = ind.get("pb")
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_detail.indicator failed for %s: %s", symbol, exc)
        if pe is None:
            degraded.append("pe_ratio")
        if pb is None:
            degraded.append("pb_ratio")

        return StockOverview(
            symbol=symbol,
            name=name,
            industry=industry,
            industry_pct_change=industry_pct,
            market_cap=market_cap,
            pe_ratio=pe,
            pb_ratio=pb,
            listing_date=listing_date,
            quote=quote,
            as_of=datetime.now().isoformat(timespec="seconds"),
            degraded_fields=degraded,
        )

    # ------------------------------------------------------------------- kline

    _PERIOD_MINUTE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "120m": "120"}

    def fetch_kline(self, symbol: str, period: str, limit: int = 250) -> dict[str, Any]:
        """获取 K 线数据。

        - ``period="D"``：本地 parquet 直读
        - ``period="W"/"M"/"Y"``：本地日线 → pandas resample
        - ``period="1m"~"120m"``：akshare 实时拉，不缓存到本地
        """
        period = period.upper() if period.upper() in ("D", "W", "M", "Y") else period.lower()
        if period == "D" or period in ("W", "M", "Y"):
            return self._fetch_kline_from_local(symbol, period, limit)
        if period in self._PERIOD_MINUTE:
            return self._fetch_kline_from_akshare_min(symbol, period, limit)
        raise ValueError(f"unsupported period: {period!r}")

    def _fetch_kline_from_local(self, symbol: str, period: str, limit: int) -> dict[str, Any]:
        # 拉一段足够长的日线，然后按需 resample + 尾部裁剪到 limit 根
        # 250 根日线 ≈ 1 年；250 周 ≈ 5 年；250 月 ≈ 20 年；250 年 ≈ 250 年。
        # 本地 parquet 一般只有 2 年数据，Y 会天然裁短，正好。
        if period == "D":
            lookback_days = int(limit * 1.8) + 30  # 交易日只占日历 ~68%
        elif period == "W":
            lookback_days = int(limit * 7 * 1.4) + 30
        elif period == "M":
            lookback_days = int(limit * 31) + 30
        else:  # Y
            lookback_days = int(limit * 366) + 30

        end = date.today()
        start = end - timedelta(days=lookback_days)
        frame = self._repo.get_ohlcv_loose([symbol], start, end)
        if frame is None or frame.empty:
            return {"symbol": symbol, "period": period, "bars": [], "source": "local_parquet", "truncated": False}

        # 只保留该 symbol（get_ohlcv_loose 已按 symbols 过滤，但保险）
        frame = frame[frame["symbol"].astype(str) == str(symbol)].copy()
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date")

        if period != "D":
            frame = _resample_ohlcv(frame, period)

        # 尾部裁剪
        if len(frame) > limit:
            frame = frame.tail(limit)

        bars = [
            {
                "t": _iso_date(row["date"]),
                "o": _num(row["open"]),
                "c": _num(row["close"]),
                "l": _num(row["low"]),
                "h": _num(row["high"]),
                "v": _num(row["volume"]),
                "a": _num(row.get("amount", 0)),
            }
            for _, row in frame.iterrows()
        ]
        return {"symbol": symbol, "period": period, "bars": bars, "source": "local_parquet", "truncated": False}

    def _fetch_kline_from_akshare_min(self, symbol: str, period: str, limit: int) -> dict[str, Any]:
        ak = self._get_ak()
        ak_period = self._PERIOD_MINUTE[period]
        # akshare stock_zh_a_hist_min_em 需要 6 位裸代码
        try:
            df = ak.stock_zh_a_hist_min_em(symbol=symbol, period=ak_period, adjust="")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"akshare minute kline failed: {exc}") from exc

        if df is None or df.empty:
            return {"symbol": symbol, "period": period, "bars": [], "source": "akshare_realtime", "truncated": False}

        # 列名映射（akshare 返回中文）
        col_map = {
            "时间": "t", "开盘": "o", "收盘": "c", "最低": "l", "最高": "h",
            "成交量": "v", "成交额": "a",
        }
        for src, dst in col_map.items():
            if src in df.columns:
                df = df.rename(columns={src: dst})

        # 只保留需要的列
        cols_needed = [c for c in ["t", "o", "c", "l", "h", "v", "a"] if c in df.columns]
        df = df[cols_needed].copy()

        # 数值列转 float，t 转字符串
        for c in ("o", "c", "l", "h", "v", "a"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if "t" in df.columns:
            df["t"] = df["t"].astype(str)

        if len(df) > limit:
            df = df.tail(limit)

        bars = [
            {
                "t": str(row.get("t", "")),
                "o": _num(row.get("o")),
                "c": _num(row.get("c")),
                "l": _num(row.get("l")),
                "h": _num(row.get("h")),
                "v": _num(row.get("v", 0)),
                "a": _num(row.get("a", 0)),
            }
            for _, row in df.iterrows()
        ]
        return {"symbol": symbol, "period": period, "bars": bars, "source": "akshare_realtime", "truncated": False}

    # ---------------------------------------------------------------- intraday

    def fetch_intraday(self, symbol: str, days: int = 1) -> dict[str, Any]:
        """分时 / 五日分时。用 akshare 1m 数据拼装。

        akshare stock_zh_a_hist_min_em 在本地网络上容易被远端切连接
        (RemoteDisconnected). 用 _ak_call_with_retry 兜底; 若最终仍失败,
        抛 RuntimeError (endpoint 层会转成 200 + empty + error).
        """
        ak = self._get_ak()
        try:
            df = _ak_call_with_retry(
                ak.stock_zh_a_hist_min_em,
                symbol=symbol, period="1", adjust="",
                label="stock_zh_a_hist_min_em",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"akshare intraday failed: {exc}") from exc

        if df is None or df.empty:
            return {"symbol": symbol, "days": days, "points": []}

        col_map = {"时间": "t", "开盘": "o", "收盘": "c", "最低": "l", "最高": "h",
                   "成交量": "v", "成交额": "a", "均价": "avg"}
        for src, dst in col_map.items():
            if src in df.columns:
                df = df.rename(columns={src: dst})

        # 5 日分时：akshare 单次一般返回近 5 交易日；1 日只保留今天
        if days == 1 and "t" in df.columns:
            try:
                df["_day"] = pd.to_datetime(df["t"]).dt.date
                today = df["_day"].max()
                df = df[df["_day"] == today].drop(columns=["_day"])
            except Exception:  # noqa: BLE001
                pass

        for c in ("c", "v", "avg"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        points = [
            {
                "t": str(row.get("t", "")),
                "price": _num(row.get("c")),
                "avg": _num(row.get("avg")) if "avg" in df.columns else None,
                "volume": _num(row.get("v", 0)),
            }
            for _, row in df.iterrows()
        ]
        return {"symbol": symbol, "days": days, "points": points}

    # ------------------------------------------------------------------ search

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        """代码前缀优先，名称包含次之。"""
        names = self._all_names()
        if not names or not query:
            return []
        q = query.strip()
        q_lower = q.lower()
        prefix_matches: list[tuple[str, str]] = []
        name_matches: list[tuple[str, str]] = []
        for sym, nm in names.items():
            if sym.startswith(q):
                prefix_matches.append((sym, nm))
                continue
            if nm and q_lower in nm.lower():
                name_matches.append((sym, nm))
        prefix_matches.sort(key=lambda x: x[0])
        name_matches.sort(key=lambda x: x[0])
        combined = (prefix_matches + name_matches)[:limit]
        return [{"symbol": s, "name": n} for s, n in combined]

    # ------------------------------------------------------------ helpers

    def _lookup_name(self, symbol: str) -> str | None:
        return self._all_names().get(str(symbol))

    def _all_names(self) -> dict[str, str]:
        if self._names_cache is not None:
            return self._names_cache
        if self._name_store is None:
            self._names_cache = {}
            return self._names_cache
        try:
            self._names_cache = self._name_store.load_all() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("StockDetailService: load_all names failed: %s", exc)
            self._names_cache = {}
        return self._names_cache

    def _fetch_spot_row(self, symbol: str) -> dict[str, Any]:
        """兼容旧调用：先拉 snapshot 再挑 row。新代码建议直接用 ``_spot_snapshot`` + ``_pick_spot_row``。"""
        df = self._spot_snapshot()
        if df is None:
            return {}
        return self._pick_spot_row(df, symbol)

    def _pick_spot_row(self, df: pd.DataFrame, symbol: str) -> dict[str, Any]:
        """从全 A 快照 df 里挑出单只股票的行，转成标准化 dict。"""
        if df is None or df.empty:
            return {}
        matched = df[df["symbol"].astype(str) == str(symbol)]
        if matched.empty:
            return {}
        row = matched.iloc[0]
        price = _num(row.get("price"))
        prev_close = _num(row.get("prev_close"))
        pct_change = None
        if price is not None and prev_close is not None and prev_close != 0:
            pct_change = (price - prev_close) / prev_close * 100.0
        return {
            "price": price,
            "pct_change": _num(row.get("pct_change")) if row.get("pct_change") is not None else pct_change,
            "open": _num(row.get("open")),
            "prev_close": prev_close,
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "volume": _num(row.get("volume")),
            "amount": _num(row.get("amount")),
            "turnover_ratio": _num(row.get("turnover_ratio")),
        }

    def _spot_snapshot(self) -> pd.DataFrame | None:
        now = time.monotonic()
        if _spot_cache["df"] is not None and now - _spot_cache["ts"] < _SPOT_CACHE_TTL_S:
            return _spot_cache["df"]
        ak = self._get_ak()
        try:
            df = ak.stock_zh_a_spot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stock_zh_a_spot fetch failed: %s", exc)
            return None
        if df is None or df.empty:
            return None
        # 列名映射到标准字段
        col_map = {
            "代码": "symbol_full", "名称": "name",
            "最新价": "price", "涨跌幅": "pct_change", "涨跌额": "pct_amount",
            "今开": "open", "昨收": "prev_close",
            "最高": "high", "最低": "low",
            "成交量": "volume", "成交额": "amount",
            "换手率": "turnover_ratio",
        }
        for src, dst in col_map.items():
            if src in df.columns:
                df = df.rename(columns={src: dst})
        # 去市场前缀
        if "symbol_full" in df.columns:
            df["symbol"] = df["symbol_full"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
        for c in ("price", "pct_change", "open", "prev_close", "high", "low", "volume", "amount", "turnover_ratio"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        _spot_cache["ts"] = now
        _spot_cache["df"] = df
        return df

    def _compute_since_listing_pct(self, symbol: str, current_price: float | None) -> float | None:
        if current_price is None:
            return None
        # 本地 parquet 通常只有近 2 年数据；至今涨幅在此定义为"本地可查最早交易日"到今天的涨幅
        end = date.today()
        start = end - timedelta(days=365 * 5)  # 保险起见拿 5 年，实际有多少给多少
        frame = self._repo.get_ohlcv_loose([symbol], start, end)
        if frame is None or frame.empty:
            return None
        frame = frame[frame["symbol"].astype(str) == str(symbol)].copy()
        if frame.empty:
            return None
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date")
        first_close = float(frame.iloc[0]["close"])
        if first_close <= 0:
            return None
        return (current_price - first_close) / first_close * 100.0

    def _compute_since_listing_pct_from_local(self, symbol: str) -> float | None:
        """至今涨幅的"纯本地估算": 用 parquet 首末收盘, 无需外部实时价.

        与 :meth:`_compute_since_listing_pct` 区别: 那个用参数传入的实时价当
        分子; 这个直接拿本地最近一根 bar 的收盘价当分子. quick 首屏用它先
        铺一个可用值, full 回来后会用真实现价重算覆盖.
        """
        end = date.today()
        start = end - timedelta(days=365 * 5)
        frame = self._repo.get_ohlcv_loose([symbol], start, end)
        if frame is None or frame.empty:
            return None
        frame = frame[frame["symbol"].astype(str) == str(symbol)].copy()
        if frame.empty:
            return None
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date")
        first_close = float(frame.iloc[0]["close"])
        last_close = float(frame.iloc[-1]["close"])
        if first_close <= 0:
            return None
        return (last_close - first_close) / first_close * 100.0

    def _first_bar_date_from_local(self, symbol: str) -> str | None:
        """本地 parquet 首个交易日, 作为 listing_date 的近似 (真实值在 akshare individual_info).

        返回 ``YYYY-MM-DD`` 字符串; 无数据返回 None.
        """
        end = date.today()
        start = end - timedelta(days=365 * 5)
        frame = self._repo.get_ohlcv_loose([symbol], start, end)
        if frame is None or frame.empty:
            return None
        frame = frame[frame["symbol"].astype(str) == str(symbol)].copy()
        if frame.empty:
            return None
        first_ts = pd.to_datetime(frame["date"]).min()
        return first_ts.strftime("%Y-%m-%d") if pd.notna(first_ts) else None

    def _fetch_individual_info(self, symbol: str) -> dict[str, Any]:
        cached = _info_cache.get(symbol)
        now = time.monotonic()
        if cached is not None and now - cached["ts"] < _INFO_CACHE_TTL_S:
            return cached["data"]
        ak = self._get_ak()
        # akshare 个股信息接口：stock_individual_info_em
        # 返回 key-value 表，字段包含 "行业" / "总市值" / "总股本" / "流通股" / "上市时间" 等
        try:
            df = _ak_call_with_retry(ak.stock_individual_info_em, symbol=symbol,
                                     label="stock_individual_info_em")
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_individual_info_em failed: %s", exc)
            _info_cache[symbol] = {"ts": now, "data": {}}
            return {}
        if df is None or df.empty:
            _info_cache[symbol] = {"ts": now, "data": {}}
            return {}
        info: dict[str, Any] = {}
        try:
            # 转成 dict：item -> value
            for _, row in df.iterrows():
                key = str(row.iloc[0]).strip()
                val = row.iloc[1]
                if key == "行业":
                    info["industry"] = str(val).strip() if val is not None else None
                elif key == "总市值":
                    info["market_cap"] = _num(val)
                elif key == "总股本":
                    info["total_shares"] = _num(val)
                elif key == "流通股":
                    info["float_shares"] = _num(val)
                elif key == "上市时间":
                    info["listing_date"] = _iso_ak_date(val)
        except Exception as exc:  # noqa: BLE001
            logger.debug("parse stock_individual_info_em failed: %s", exc)
        _info_cache[symbol] = {"ts": now, "data": info}
        return info

    def _fetch_indicator(self, symbol: str) -> dict[str, Any]:
        cached = _indicator_cache.get(symbol)
        now = time.monotonic()
        if cached is not None and now - cached["ts"] < _INDICATOR_CACHE_TTL_S:
            return cached["data"]
        ak = self._get_ak()
        try:
            df = _ak_call_with_retry(ak.stock_a_lg_indicator, symbol=symbol,
                                     label="stock_a_lg_indicator")
        except Exception as exc:  # noqa: BLE001
            logger.debug("stock_a_lg_indicator failed: %s", exc)
            _indicator_cache[symbol] = {"ts": now, "data": {}}
            return {}
        if df is None or df.empty:
            _indicator_cache[symbol] = {"ts": now, "data": {}}
            return {}
        # 取最新一行
        try:
            row = df.iloc[-1]
            data = {
                "pe": _num(row.get("pe") or row.get("pe_ttm")),
                "pb": _num(row.get("pb")),
                "ps": _num(row.get("ps") or row.get("ps_ttm")),
                "dv_ratio": _num(row.get("dv_ratio")),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("parse indicator failed: %s", exc)
            data = {}
        _indicator_cache[symbol] = {"ts": now, "data": data}
        return data

    def _fetch_industry_pct(self, industry_name: str) -> float | None:
        now = time.monotonic()
        if _sw_cache["df"] is None or now - _sw_cache["ts"] > _SW_CACHE_TTL_S:
            ak = self._get_ak()
            try:
                df = _ak_call_with_retry(ak.sw_index_first_info, label="sw_index_first_info")
            except Exception as exc:  # noqa: BLE001
                logger.debug("sw_index_first_info failed: %s", exc)
                return None
            if df is None or df.empty:
                return None
            _sw_cache["df"] = df
            _sw_cache["ts"] = now
        df = _sw_cache["df"]
        # akshare 返回列一般含"行业名称" / "涨跌幅"
        candidate_name_cols = ["行业名称", "指数名称", "板块名称", "name"]
        candidate_pct_cols = ["涨跌幅", "chg_pct", "pct_change"]
        name_col = next((c for c in candidate_name_cols if c in df.columns), None)
        pct_col = next((c for c in candidate_pct_cols if c in df.columns), None)
        if name_col is None or pct_col is None:
            return None
        matched = df[df[name_col].astype(str).str.contains(industry_name, na=False, regex=False)]
        if matched.empty:
            return None
        return _num(matched.iloc[0][pct_col])

    def _get_ak(self) -> Any:
        if self._ak is not None:
            return self._ak
        import akshare as ak
        self._ak = ak
        return ak


# ============================================================================
# 独立辅助函数（可 import 供 stock_analyst_agent 复用）
# ============================================================================


def _resample_ohlcv(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    """把日线 resample 成周/月/年。frame 必须已含 datetime dtype 的 date 列。"""
    rule_map = {"W": "W-FRI", "M": "ME", "Y": "YE"}
    rule = rule_map[period]
    df = frame.set_index("date")
    agg: dict[str, Any] = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "amount" in df.columns:
        agg["amount"] = "sum"
    resampled = df.resample(rule).agg(agg).dropna(subset=["open", "close"])
    resampled = resampled.reset_index()
    return resampled


def _num(v: Any) -> float | None:
    """把可能是 NaN / None / 空字符串 / 字符串数字 都转成 float 或 None。"""
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.strip()
            if v == "" or v == "-" or v.lower() == "nan":
                return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (ValueError, TypeError):
        return None


def _iso_date(v: Any) -> str:
    """把 date / datetime / Timestamp 转 YYYY-MM-DD 字符串。"""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def _iso_ak_date(v: Any) -> str | None:
    """akshare 返回的 listing_date 可能是 int (20110127) / str / Timestamp。"""
    if v is None:
        return None
    try:
        s = str(int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
        s = s.strip()
        if not s or s.lower() == "nan":
            return None
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s[:10]
    except Exception:  # noqa: BLE001
        return None


def compute_ma(closes: list[float], window: int) -> list[float | None]:
    """给 stock_analyst_agent 用的 MA 计算工具（前端也会另实现一份 JS 版）。"""
    out: list[float | None] = []
    if not closes:
        return out
    for i in range(len(closes)):
        if i + 1 < window:
            out.append(None)
            continue
        window_vals = [c for c in closes[i + 1 - window : i + 1] if c is not None]
        if len(window_vals) < window:
            out.append(None)
        else:
            out.append(sum(window_vals) / window)
    return out


def compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """标准 MACD 计算，用于后端 AI prompt。返回 (dif, dea, macd_hist)。"""
    if not closes:
        return [], [], []
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    dif = [
        (fe - se) if (fe is not None and se is not None) else None
        for fe, se in zip(fast_ema, slow_ema, strict=False)
    ]
    dea = _ema([d if d is not None else 0.0 for d in dif], signal)
    macd_hist = [
        ((d - de) * 2) if (d is not None and de is not None) else None
        for d, de in zip(dif, dea, strict=False)
    ]
    return dif, dea, macd_hist


def _ema(values: list[float], period: int) -> list[float | None]:
    """标准 EMA：k = 2 / (period + 1)。前 period-1 根返回 None，第 period 根用 SMA 起价。"""
    out: list[float | None] = []
    if not values:
        return out
    k = 2.0 / (period + 1)
    ema_prev: float | None = None
    for i, v in enumerate(values):
        if i + 1 < period:
            out.append(None)
            continue
        if i + 1 == period:
            ema_prev = sum(values[:period]) / period
        else:
            assert ema_prev is not None
            ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out
