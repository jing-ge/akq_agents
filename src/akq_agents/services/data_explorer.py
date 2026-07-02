"""AKShare 数据探索服务（M10）。

设计原则：
- **白名单**：只暴露明确测试可用的接口（避开东方财富网络受限的接口）。
- **声明式 schema**：每个接口在 _CATALOG 里声明 → 入参 / 出参 / 展示类型。
- **5 分钟 TTL 内存缓存**：(api, args_tuple) → DataFrame；避免重复请求。
- **类型安全的渲染 hint**：返回带 `display_hint = chart_kline / chart_line / table` 等给前端。

不持久化、不写库；纯展示用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300  # 5 分钟


# ----------------- catalog --------------------------------------------------


@dataclass
class ParamSpec:
    name: str
    type: str = "string"   # string / int / date / enum
    label: str = ""
    required: bool = False
    default: Any = None
    options: list[str] | None = None  # enum
    help: str = ""


@dataclass
class ApiSpec:
    """一个 akshare 接口的声明。"""

    api: str                       # akshare 函数名，例如 stock_zh_a_daily
    label: str                     # 中文显示名
    category: str                  # 行情 / 指数 / 板块 / 财务 / 资金 / 宏观 / 情绪 / 工具
    desc: str                      # 一行说明
    params: list[ParamSpec] = field(default_factory=list)
    display_hint: str = "table"    # table / chart_kline / chart_line / chart_bar
    chart_x: str | None = None     # 图表用：x 轴列名
    chart_y: list[str] | None = None   # 图表用：y 轴列名（line/bar）
    kline_cols: dict[str, str] | None = None  # 图表用：K 线列映射 {open/close/low/high/volume}
    # 后处理（akshare 返回往往含中文列名）
    rename: dict[str, str] | None = None
    head: int | None = None        # 截取前 N 行（避免一次返回上万行）


# 白名单接口列表
_CATALOG: list[ApiSpec] = [
    # ============ 个股行情 ============
    ApiSpec(
        api="stock_zh_a_daily",
        label="个股日线（新浪复权）",
        category="个股行情",
        desc="单只 A 股的日线 OHLCV + 前复权 / 后复权，新浪源稳定。",
        params=[
            ParamSpec("symbol", "string", "代码", required=True, default="sh600519",
                     help="sh/sz/bj 前缀 + 6 位代码，例如 sh600519、sz000001、bj430047"),
            ParamSpec("adjust", "enum", "复权", default="qfq", options=["qfq", "hfq", ""]),
        ],
        display_hint="chart_kline",
        kline_cols={"date": "date", "open": "open", "close": "close",
                    "low": "low", "high": "high", "volume": "volume"},
        head=250,
    ),
    ApiSpec(
        api="stock_zh_a_spot",
        label="今日全 A 股行情快照（新浪）",
        category="个股行情",
        desc="全部 A 股的当前价 / 涨跌幅 / 成交量等，可按列排序看市场异动。",
        params=[],
        display_hint="table",
        head=200,
    ),

    # ============ 指数 ============
    ApiSpec(
        api="stock_zh_index_daily",
        label="指数日线（新浪）",
        category="指数",
        desc="国内主要指数的历史日线（沪深300、中证500、创业板指等）。",
        params=[
            ParamSpec("symbol", "string", "指数代码", required=True, default="sh000300",
                     help="sh000300=沪深300, sh000905=中证500, sz399006=创业板指, sh000016=上证50"),
        ],
        display_hint="chart_line",
        chart_x="date",
        chart_y=["close"],
        head=500,
    ),
    ApiSpec(
        api="stock_zh_index_spot_sina",
        label="今日全指数行情（新浪）",
        category="指数",
        desc="全部 A 股指数当前价 + 涨跌幅，扫描市场整体格局。",
        params=[],
        display_hint="table",
        head=100,
    ),

    # ============ 申万行业 ============
    ApiSpec(
        api="sw_index_first_info",
        label="申万一级行业列表",
        category="板块",
        desc="31 个申万一级行业及对应指数代码 / 成份股数 / 估值。",
        params=[],
        display_hint="table",
    ),
    ApiSpec(
        api="index_component_sw",
        label="申万行业成份股",
        category="板块",
        desc="某申万一级行业的成份股清单（含权重 + 计入日期）。",
        params=[
            ParamSpec("symbol", "string", "行业代码", required=True, default="801080",
                     help="801010=农林牧渔, 801030=基础化工, 801080=电子, 801750=计算机..."),
        ],
        display_hint="table",
        head=200,
    ),
    ApiSpec(
        api="index_hist_sw",
        label="申万行业指数历史",
        category="板块",
        desc="申万行业指数的日线 / 周线 / 月线数据。",
        params=[
            ParamSpec("symbol", "string", "行业代码", required=True, default="801080"),
            ParamSpec("period", "enum", "周期", default="day", options=["day", "week", "month"]),
        ],
        display_hint="chart_line",
        chart_x="日期",
        chart_y=["收盘"],
        head=500,
    ),

    # ============ 市场快照 / 交易所 ============
    ApiSpec(
        api="stock_sse_summary",
        label="上交所市场总貌",
        category="市场快照",
        desc="上交所上市公司数 / 市价总值 / 平均市盈率等。",
        params=[],
        display_hint="table",
    ),
    ApiSpec(
        api="stock_szse_summary",
        label="深交所市场总貌",
        category="市场快照",
        desc="深交所主板/创业板/科创板细分股票数 + 总市值。",
        params=[],
        display_hint="table",
    ),

    # ============ 宏观 ============
    ApiSpec(
        api="macro_china_cpi",
        label="中国 CPI 历史",
        category="宏观",
        desc="月度 CPI 同比 / 环比。",
        params=[],
        display_hint="chart_line",
        chart_x="月份",
        chart_y=["全国-当月"],
        head=120,
    ),
    ApiSpec(
        api="macro_china_ppi_yearly",
        label="中国 PPI 历史",
        category="宏观",
        desc="月度 PPI 同比。",
        params=[],
        display_hint="chart_line",
        head=120,
    ),
    ApiSpec(
        api="macro_china_pmi_yearly",
        label="中国 PMI 历史",
        category="宏观",
        desc="月度制造业 PMI。",
        params=[],
        display_hint="chart_line",
        head=120,
    ),
    ApiSpec(
        api="macro_china_gdp_yearly",
        label="中国 GDP 同比",
        category="宏观",
        desc="季度 GDP 同比增速。",
        params=[],
        display_hint="chart_line",
        chart_x="日期",
        chart_y=["今值", "预测值", "前值"],
        head=120,
    ),
    ApiSpec(
        api="macro_china_money_supply",
        label="中国货币供应量",
        category="宏观",
        desc="M0 / M1 / M2 月度数据。",
        params=[],
        display_hint="table",
        head=120,
    ),

    # ============ 情绪 / 新闻 ============
    ApiSpec(
        api="news_economic_baidu",
        label="百度财经-财经日历",
        category="情绪/新闻",
        desc="今日 / 近期重要财经事件（中美利率决议、CPI 公布等）。",
        params=[],
        display_hint="table",
        head=80,
    ),
    ApiSpec(
        api="stock_news_main_cx",
        label="财新主要新闻",
        category="情绪/新闻",
        desc="财新网最新主要新闻列表。",
        params=[],
        display_hint="table",
        head=50,
    ),

    # ============ 工具 ============
    ApiSpec(
        api="tool_trade_date_hist_sina",
        label="交易日历（日历视图）",
        category="工具",
        desc="A 股交易日历：绿色=交易日、灰色=非交易日，按年切换。",
        params=[
            ParamSpec("year", "int", "年份", default=2026,
                     help="例如 2025、2026；可查 1990 起任意年"),
        ],
        display_hint="calendar",
    ),
]


# ----------------- service --------------------------------------------------


class DataExplorerService:
    """带 TTL 缓存的 akshare 接口代理。"""

    def __init__(self, ttl_seconds: int = _TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self._catalog: dict[str, ApiSpec] = {spec.api: spec for spec in _CATALOG}

    def list_catalog(self) -> list[dict]:
        """返回前端展示用的接口目录（按 category 分组）。"""
        return [
            {
                "api": s.api,
                "label": s.label,
                "category": s.category,
                "desc": s.desc,
                "display_hint": s.display_hint,
                "params": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "label": p.label,
                        "required": p.required,
                        "default": p.default,
                        "options": p.options,
                        "help": p.help,
                    }
                    for p in s.params
                ],
            }
            for s in _CATALOG
        ]

    def fetch(self, api: str, args: dict[str, Any]) -> dict[str, Any]:
        """拉取 + 缓存 + 后处理。"""
        spec = self._catalog.get(api)
        if spec is None:
            return {"error": "UNKNOWN_API", "detail": f"api {api!r} 未在白名单"}

        clean_args = self._clean_args(spec, args)
        cache_key = (api, tuple(sorted(clean_args.items())))
        now = time.monotonic()
        if cache_key in self._cache:
            cached_at, df = self._cache[cache_key]
            if now - cached_at < self._ttl:
                return self._format_response(spec, df, from_cache=True)

        # 实际拉取
        import akshare as ak

        fn = getattr(ak, api, None)
        if fn is None:
            return {"error": "API_NOT_FOUND", "detail": f"akshare 无 {api}"}

        # 特殊处理：交易日历 akshare 不接收 year 参数，拉全量后再按 year 筛
        post_filter_year: int | None = None
        if api == "tool_trade_date_hist_sina":
            post_filter_year = clean_args.pop("year", None)

        try:
            df = fn(**clean_args) if clean_args else fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("akshare %s failed: %s", api, exc)
            return {"error": "FETCH_FAILED", "detail": str(exc)[:300]}

        if not isinstance(df, pd.DataFrame):
            # 有些接口返回 dict / list；统一转 DataFrame
            try:
                df = pd.DataFrame(df)
            except Exception:
                return {"error": "BAD_RESPONSE", "detail": "akshare 返回非 DataFrame"}

        # 交易日历：按 year 筛
        if post_filter_year is not None and "trade_date" in df.columns:
            try:
                df = df.copy()
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df = df[df["trade_date"].dt.year == int(post_filter_year)]
                df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        self._cache[cache_key] = (now, df)
        return self._format_response(spec, df, from_cache=False)

    @staticmethod
    def _clean_args(spec: ApiSpec, args: dict[str, Any]) -> dict[str, Any]:
        """按 spec.params 过滤 + 类型转换。"""
        out: dict[str, Any] = {}
        for p in spec.params:
            v = args.get(p.name)
            if v is None or v == "":
                if p.default is not None:
                    out[p.name] = p.default
                continue
            if p.type == "int":
                try:
                    out[p.name] = int(v)
                except (ValueError, TypeError):
                    continue
            else:
                out[p.name] = v
        return out

    @staticmethod
    def _format_response(spec: ApiSpec, df: pd.DataFrame, from_cache: bool) -> dict[str, Any]:
        if df is None or df.empty:
            return {
                "api": spec.api,
                "label": spec.label,
                "display_hint": spec.display_hint,
                "n_rows": 0,
                "columns": [],
                "rows": [],
                "from_cache": from_cache,
                "message": "返回空数据集",
            }
        # rename
        if spec.rename:
            df = df.rename(columns=spec.rename)
        # head
        total = len(df)
        if spec.head and total > spec.head:
            # 对时序数据保留最后 N 行；对其它保留前 N 行
            if spec.display_hint in ("chart_kline", "chart_line") and "date" in df.columns:
                df_show = df.tail(spec.head)
            elif spec.display_hint in ("chart_line",) and "日期" in df.columns:
                df_show = df.tail(spec.head)
            else:
                df_show = df.head(spec.head)
        else:
            df_show = df

        # 转纯 JSON 友好类型
        df_show = df_show.copy()
        for col in df_show.columns:
            if df_show[col].dtype.kind in ("i", "u", "f"):
                df_show[col] = df_show[col].astype(float).where(df_show[col].notna(), None)
            elif df_show[col].dtype.kind in ("M",):  # datetime
                df_show[col] = df_show[col].astype(str)
            else:
                df_show[col] = df_show[col].astype(str)

        rows = df_show.to_dict(orient="records")
        return {
            "api": spec.api,
            "label": spec.label,
            "category": spec.category,
            "desc": spec.desc,
            "display_hint": spec.display_hint,
            "n_rows": len(rows),
            "n_total": total,
            "truncated": total > len(rows),
            "columns": list(df_show.columns),
            "rows": rows,
            "kline_cols": spec.kline_cols,
            "chart_x": spec.chart_x,
            "chart_y": spec.chart_y,
            "from_cache": from_cache,
        }
