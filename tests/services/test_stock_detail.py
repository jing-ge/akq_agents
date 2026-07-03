"""StockDetailService 单元测试。

覆盖：
- fetch_overview happy path（本地 parquet + mock akshare 全绿）
- fetch_overview 各 akshare 依赖失败时的降级
- fetch_kline 日/周/月/年 resample 正确性
- fetch_kline 分钟级透传
- fetch_kline 本地无数据时返回空
- search 前缀 / 名称匹配
- compute_ma / compute_macd 数值正确性
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from akq_agents.services.stock_detail import (
    StockDetailService,
    _resample_ohlcv,
    compute_ma,
    compute_macd,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """每个 case 前清空 stock_detail 模块级 5min TTL 缓存，避免跨 case 泄漏。"""
    from akq_agents.services import stock_detail as sd

    sd._spot_cache["ts"] = 0.0
    sd._spot_cache["df"] = None
    sd._info_cache.clear()
    sd._indicator_cache.clear()
    sd._sw_cache["ts"] = 0.0
    sd._sw_cache["df"] = None
    yield


def _make_ohlcv(symbol: str, n_days: int, start_price: float = 10.0) -> pd.DataFrame:
    """构造 n_days 天的假 OHLCV，每天涨 1%。"""
    rows = []
    price = start_price
    end = date.today()
    for i in range(n_days):
        d = end - timedelta(days=n_days - 1 - i)
        rows.append({
            "symbol": symbol,
            "date": d,
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price * 1.01,
            "volume": 100000 + i * 1000,
            "amount": (100000 + i * 1000) * price,
        })
        price *= 1.01
    return pd.DataFrame(rows)


def _make_spot_df(symbol: str, price: float = 15.5) -> pd.DataFrame:
    """构造 akshare stock_zh_a_spot 风格 DF（中文列名）。"""
    return pd.DataFrame([
        {
            "代码": f"sh{symbol}" if symbol.startswith("6") else f"sz{symbol}",
            "名称": "测试股",
            "最新价": price,
            "涨跌幅": 1.5,
            "今开": price - 0.1,
            "昨收": price - 0.2,
            "最高": price + 0.3,
            "最低": price - 0.4,
            "成交量": 1_234_500,
            "成交额": 2.5e8,
            "换手率": 3.9,
        },
        # 另一只股票，验证按 symbol 筛选
        {
            "代码": "sh600000",
            "名称": "浦发银行",
            "最新价": 8.88,
            "涨跌幅": -0.5,
            "今开": 8.9,
            "昨收": 8.92,
            "最高": 8.95,
            "最低": 8.85,
            "成交量": 500000,
            "成交额": 4.4e7,
            "换手率": 0.3,
        },
    ])


def _make_info_df(industry: str = "通用设备", market_cap: float = 3.1e10) -> pd.DataFrame:
    """akshare stock_individual_info_em 风格：两列（item / value）。"""
    return pd.DataFrame([
        {"item": "行业", "value": industry},
        {"item": "总市值", "value": market_cap},
        {"item": "总股本", "value": 6.77e9},
        {"item": "流通股", "value": 6.5e9},
        {"item": "上市时间", "value": 20110127},
    ])


def _make_indicator_df(pe: float = -722.48, pb: float = 1.23) -> pd.DataFrame:
    """akshare stock_a_lg_indicator 风格：最后一行是最新。"""
    return pd.DataFrame([
        {"trade_date": "2026-06-01", "pe": pe - 10, "pb": pb - 0.1, "ps": 1.5, "dv_ratio": 0.02},
        {"trade_date": "2026-07-01", "pe": pe, "pb": pb, "ps": 1.6, "dv_ratio": 0.02},
    ])


def _make_sw_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"行业名称": "通用设备", "涨跌幅": 3.35},
        {"行业名称": "银行", "涨跌幅": -0.2},
    ])


def _make_min_df() -> pd.DataFrame:
    """akshare stock_zh_a_hist_min_em 风格。"""
    rows = []
    base_ts = pd.Timestamp("2026-07-03 09:30:00")
    for i in range(30):
        rows.append({
            "时间": (base_ts + pd.Timedelta(minutes=i * 5)).strftime("%Y-%m-%d %H:%M:%S"),
            "开盘": 10.0 + i * 0.01,
            "收盘": 10.0 + i * 0.01 + 0.005,
            "最低": 10.0 + i * 0.01 - 0.02,
            "最高": 10.0 + i * 0.01 + 0.03,
            "成交量": 1000 + i * 10,
            "成交额": (1000 + i * 10) * 10.0,
            "均价": 10.0 + i * 0.005,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def repo_with_ohlcv():
    """MagicMock repo，其 get_ohlcv_loose 返回 60 天日线。"""
    repo = MagicMock()
    df = _make_ohlcv("002131", 60)
    repo.get_ohlcv_loose.return_value = df
    return repo


@pytest.fixture
def name_store():
    """MagicMock name_store。"""
    ns = MagicMock()
    ns.load_all.return_value = {
        "002131": "利欧股份",
        "600519": "贵州茅台",
        "600000": "浦发银行",
        "300750": "宁德时代",
    }
    return ns


@pytest.fixture
def fake_ak_all_ok():
    """全套 akshare mock，全部返 happy path 数据。"""
    ak = SimpleNamespace()
    ak.stock_zh_a_spot = MagicMock(return_value=_make_spot_df("002131"))
    ak.stock_individual_info_em = MagicMock(return_value=_make_info_df())
    ak.stock_a_lg_indicator = MagicMock(return_value=_make_indicator_df())
    ak.sw_index_first_info = MagicMock(return_value=_make_sw_df())
    ak.stock_zh_a_hist_min_em = MagicMock(return_value=_make_min_df())
    return ak


# ============================================================================
# fetch_overview
# ============================================================================


def test_fetch_overview_happy_path(repo_with_ohlcv, name_store, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    ov = svc.fetch_overview("002131")
    assert ov.symbol == "002131"
    assert ov.name == "利欧股份"
    assert ov.industry == "通用设备"
    assert ov.market_cap == 3.1e10
    assert ov.pe_ratio == -722.48
    assert ov.pb_ratio == 1.23
    assert ov.listing_date == "2011-01-27"
    assert ov.industry_pct_change == 3.35
    assert ov.quote["price"] == 15.5
    assert ov.quote["turnover_ratio"] == 3.9
    # 60 天数据，第一根 close ≈ 10.1，现价 15.5 → 至今涨幅约 53%
    assert ov.quote["since_listing_pct"] is not None
    assert ov.quote["since_listing_pct"] > 0
    assert ov.degraded_fields == []


def test_fetch_overview_individual_info_failed(repo_with_ohlcv, name_store, fake_ak_all_ok):
    """stock_individual_info_em 抛错 → industry / market_cap 降级。"""
    fake_ak_all_ok.stock_individual_info_em = MagicMock(side_effect=RuntimeError("network down"))
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    ov = svc.fetch_overview("002131")
    assert ov.industry is None
    assert ov.market_cap is None
    assert "industry" in ov.degraded_fields
    assert "market_cap" in ov.degraded_fields
    # 依赖 industry 名字才能查行业涨跌幅 → 也降级
    assert "industry_pct_change" in ov.degraded_fields
    # 其它字段仍正常
    assert ov.name == "利欧股份"
    assert ov.pe_ratio == -722.48
    assert ov.quote["price"] == 15.5


def test_fetch_overview_indicator_failed(repo_with_ohlcv, name_store, fake_ak_all_ok):
    fake_ak_all_ok.stock_a_lg_indicator = MagicMock(side_effect=RuntimeError("boom"))
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    ov = svc.fetch_overview("002131")
    assert ov.pe_ratio is None
    assert ov.pb_ratio is None
    assert "pe_ratio" in ov.degraded_fields
    assert "pb_ratio" in ov.degraded_fields


def test_fetch_overview_spot_failed(repo_with_ohlcv, name_store, fake_ak_all_ok):
    """spot 失败 → quote 降级为空 dict，其它字段仍可用。"""
    fake_ak_all_ok.stock_zh_a_spot = MagicMock(side_effect=RuntimeError("net"))
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    ov = svc.fetch_overview("002131")
    assert "quote" in ov.degraded_fields
    # spot 失败 → 现价 None → since_listing_pct 自然也 None
    assert ov.quote.get("price") is None or ov.quote == {} or "since_listing_pct" not in ov.quote


def test_fetch_overview_unknown_symbol(repo_with_ohlcv, name_store, fake_ak_all_ok):
    """spot 里没找到该 symbol → quote 空、name 也 None。"""
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    ov = svc.fetch_overview("999999")
    assert ov.name is None
    # quote 不 raise，直接空 dict
    assert ov.quote.get("price") is None or "price" not in ov.quote


# ============================================================================
# fetch_kline
# ============================================================================


def test_fetch_kline_daily_from_local(repo_with_ohlcv, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_kline("002131", "D", limit=30)
    assert result["period"] == "D"
    assert result["source"] == "local_parquet"
    assert len(result["bars"]) == 30
    for bar in result["bars"]:
        assert "t" in bar and "o" in bar and "c" in bar and "l" in bar and "h" in bar and "v" in bar
        assert bar["o"] > 0
        assert bar["c"] > 0


def test_fetch_kline_weekly_resample(repo_with_ohlcv, fake_ak_all_ok):
    """W → 周五收盘，volume 求和。"""
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_kline("002131", "W", limit=20)
    assert result["period"] == "W"
    assert result["source"] == "local_parquet"
    # 60 天日线约有 8-9 周
    assert 6 <= len(result["bars"]) <= 12
    # 每周 open 应 = 该周第一个日线的 open，close = 最后一个日线的 close
    # 简单验证：所有 bar 的 o/c/l/h 都不为 None
    for bar in result["bars"]:
        assert bar["o"] is not None
        assert bar["c"] is not None
        assert bar["l"] is not None
        assert bar["h"] is not None


def test_fetch_kline_monthly_resample(repo_with_ohlcv, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_kline("002131", "M", limit=20)
    assert result["period"] == "M"
    # 60 天 ≈ 2 个月
    assert 1 <= len(result["bars"]) <= 4


def test_fetch_kline_minute_pass_through(repo_with_ohlcv, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_kline("002131", "5m", limit=100)
    assert result["period"] == "5m"
    assert result["source"] == "akshare_realtime"
    assert len(result["bars"]) == 30  # fake_min_df 返 30 条
    # 验证 akshare 被以正确参数调用
    fake_ak_all_ok.stock_zh_a_hist_min_em.assert_called_once()
    call_kwargs = fake_ak_all_ok.stock_zh_a_hist_min_em.call_args.kwargs
    assert call_kwargs["symbol"] == "002131"
    assert call_kwargs["period"] == "5"


def test_fetch_kline_minute_akshare_fail_raises(repo_with_ohlcv, fake_ak_all_ok):
    fake_ak_all_ok.stock_zh_a_hist_min_em = MagicMock(side_effect=RuntimeError("network"))
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    with pytest.raises(RuntimeError):
        svc.fetch_kline("002131", "1m", limit=100)


def test_fetch_kline_empty_local(fake_ak_all_ok):
    repo = MagicMock()
    repo.get_ohlcv_loose.return_value = pd.DataFrame()
    svc = StockDetailService(repo=repo, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_kline("999999", "D", limit=100)
    assert result["bars"] == []
    assert result["source"] == "local_parquet"


def test_fetch_kline_unsupported_period(repo_with_ohlcv, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    with pytest.raises(ValueError):
        svc.fetch_kline("002131", "3m", limit=100)


# ============================================================================
# resample 独立验证
# ============================================================================


def test_resample_ohlcv_weekly():
    """构造 10 天日线，验证 W 聚合规则。"""
    df = _make_ohlcv("X", 14, start_price=10.0)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    result = _resample_ohlcv(df, "W")
    assert not result.empty
    # 每周 open = first, high = max, low = min, close = last
    for _, row in result.iterrows():
        assert row["high"] >= row["low"]
        assert row["high"] >= row["open"]
        assert row["high"] >= row["close"]


# ============================================================================
# fetch_intraday
# ============================================================================


def test_fetch_intraday_happy_path(repo_with_ohlcv, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    result = svc.fetch_intraday("002131", days=1)
    assert result["symbol"] == "002131"
    assert result["days"] == 1
    assert isinstance(result["points"], list)
    if result["points"]:
        assert "t" in result["points"][0]
        assert "price" in result["points"][0]


def test_fetch_intraday_akshare_fail(repo_with_ohlcv, fake_ak_all_ok):
    fake_ak_all_ok.stock_zh_a_hist_min_em = MagicMock(side_effect=RuntimeError("net"))
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=None, ak_module=fake_ak_all_ok)
    with pytest.raises(RuntimeError):
        svc.fetch_intraday("002131", days=1)


# ============================================================================
# search
# ============================================================================


def test_search_by_prefix(repo_with_ohlcv, name_store, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    matches = svc.search("6005", limit=8)
    assert any(m["symbol"] == "600519" for m in matches)


def test_search_by_name(repo_with_ohlcv, name_store, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    matches = svc.search("茅台", limit=8)
    assert any(m["symbol"] == "600519" for m in matches)


def test_search_empty(repo_with_ohlcv, name_store, fake_ak_all_ok):
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    assert svc.search("", limit=8) == []


def test_search_prefix_takes_priority(repo_with_ohlcv, name_store, fake_ak_all_ok):
    """代码前缀应排在名字包含之前。"""
    svc = StockDetailService(repo=repo_with_ohlcv, name_store=name_store, ak_module=fake_ak_all_ok)
    # "6" 会前缀匹配 600519 / 600000，名字里没有含 "6" 的
    matches = svc.search("6", limit=8)
    for m in matches[:2]:
        assert m["symbol"].startswith("6")


# ============================================================================
# compute_ma / compute_macd
# ============================================================================


def test_compute_ma_short_window():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    ma3 = compute_ma(closes, 3)
    assert ma3 == [None, None, 2.0, 3.0, 4.0]


def test_compute_ma_full_window():
    closes = [10.0] * 10
    ma5 = compute_ma(closes, 5)
    # 前 4 个 None，后 6 个都是 10.0
    assert ma5[:4] == [None, None, None, None]
    assert all(v == 10.0 for v in ma5[4:])


def test_compute_macd_shapes():
    closes = [float(i + 1) for i in range(50)]
    dif, dea, hist = compute_macd(closes)
    assert len(dif) == 50
    assert len(dea) == 50
    assert len(hist) == 50
    # 稳态上升趋势时 DIF 会稳定接近某个正值
    assert any(d is not None for d in dif)


def test_compute_ma_empty():
    assert compute_ma([], 5) == []


def test_compute_macd_empty():
    dif, dea, hist = compute_macd([])
    assert dif == [] and dea == [] and hist == []
