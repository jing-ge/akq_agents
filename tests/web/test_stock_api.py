"""Stock API endpoints 集成测试（FastAPI TestClient）。

覆盖：
- GET /api/stock/overview/{symbol} — happy path + 降级
- GET /api/stock/kline/{symbol}?period=D/W/1m
- GET /api/stock/intraday/{symbol}?days=1
- POST /api/stock/analyze/{symbol} — mock LLMOrchestrator，验证 session 写入
- POST /api/stock/analyze/{symbol} — LLMGatewayError → 502
- GET /api/stock/search?q=...
- symbol 校验（400）
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _reset_stock_singleton():
    """每个 case 前重置 stock router 的 service 单例，让 fixture 生效。"""
    from akq_agents.web.api import stock as stock_api

    stock_api._reset_service_for_tests()
    # 同时清空 stock_detail 模块级 5min TTL 缓存
    from akq_agents.services import stock_detail as sd

    sd._spot_cache["ts"] = 0.0
    sd._spot_cache["df"] = None
    sd._info_cache.clear()
    sd._indicator_cache.clear()
    sd._sw_cache["ts"] = 0.0
    sd._sw_cache["df"] = None
    yield
    stock_api._reset_service_for_tests()


@pytest.fixture
def stock_service_stub(assets, monkeypatch):
    """替换 stock router 里的 _get_service，让它返回一个可控的 mock StockDetailService。"""
    from akq_agents.web.api import stock as stock_api

    svc_mock = MagicMock()
    monkeypatch.setattr(stock_api, "_get_service", lambda: svc_mock)
    return svc_mock


# ============================================================================
# /api/stock/overview
# ============================================================================


def test_overview_happy_path(client, stock_service_stub) -> None:
    from akq_agents.services.stock_detail import StockOverview

    stock_service_stub.fetch_overview.return_value = StockOverview(
        symbol="002131",
        name="利欧股份",
        industry="通用设备",
        industry_pct_change=3.35,
        market_cap=3.1e10,
        pe_ratio=-722.48,
        pb_ratio=1.23,
        listing_date="2011-01-27",
        quote={"price": 4.59, "pct_change": -0.43, "open": 4.60, "prev_close": 4.61,
               "high": 4.63, "low": 4.51, "volume": 2281900, "amount": 1.040e9,
               "turnover_ratio": 3.9, "since_listing_pct": -37.12},
        as_of="2026-07-03T15:00:00",
        degraded_fields=[],
    )
    r = client.get("/api/stock/overview/002131")
    assert r.status_code == 200
    d = r.json()
    assert d["symbol"] == "002131"
    assert d["name"] == "利欧股份"
    assert d["industry"] == "通用设备"
    assert d["quote"]["price"] == 4.59
    assert d["degraded_fields"] == []


def test_overview_degraded_still_200(client, stock_service_stub) -> None:
    from akq_agents.services.stock_detail import StockOverview

    stock_service_stub.fetch_overview.return_value = StockOverview(
        symbol="002131",
        name="利欧股份",
        industry=None,
        industry_pct_change=None,
        market_cap=None,
        pe_ratio=None,
        pb_ratio=None,
        listing_date=None,
        quote={"price": 4.59, "pct_change": -0.43},
        as_of="2026-07-03T15:00:00",
        degraded_fields=["industry", "market_cap", "pe_ratio", "pb_ratio"],
    )
    r = client.get("/api/stock/overview/002131")
    assert r.status_code == 200
    d = r.json()
    assert d["industry"] is None
    assert "industry" in d["degraded_fields"]
    assert d["quote"]["price"] == 4.59


def test_overview_symbol_with_prefix_ok(client, stock_service_stub) -> None:
    """sh002131 前缀应被剥掉。"""
    from akq_agents.services.stock_detail import StockOverview

    stock_service_stub.fetch_overview.return_value = StockOverview(
        symbol="002131", name="利欧股份", industry=None, industry_pct_change=None,
        market_cap=None, pe_ratio=None, pb_ratio=None, listing_date=None,
        quote={}, as_of="2026-07-03T15:00:00", degraded_fields=[],
    )
    r = client.get("/api/stock/overview/sz002131")
    assert r.status_code == 200
    # 验证 endpoint 剥掉了 sz 前缀
    args, _ = stock_service_stub.fetch_overview.call_args
    assert args[0] == "002131"


def test_overview_invalid_symbol_400(client, stock_service_stub) -> None:
    r = client.get("/api/stock/overview/ABCDEF")
    assert r.status_code == 400


def test_overview_short_symbol_400(client, stock_service_stub) -> None:
    r = client.get("/api/stock/overview/123")
    assert r.status_code == 400


# ============================================================================
# /api/stock/kline
# ============================================================================


def test_kline_daily_default(client, stock_service_stub) -> None:
    stock_service_stub.fetch_kline.return_value = {
        "symbol": "002131", "period": "D", "source": "local_parquet",
        "bars": [{"t": "2026-07-01", "o": 4.5, "c": 4.6, "l": 4.48, "h": 4.65, "v": 1000, "a": 4600.0}],
        "truncated": False,
    }
    r = client.get("/api/stock/kline/002131")
    assert r.status_code == 200
    d = r.json()
    assert d["period"] == "D"
    assert len(d["bars"]) == 1
    _, kwargs = stock_service_stub.fetch_kline.call_args
    # positional 或 kw 传入都可以
    all_args = list(stock_service_stub.fetch_kline.call_args.args) + list(kwargs.values())
    assert "D" in all_args


def test_kline_weekly(client, stock_service_stub) -> None:
    stock_service_stub.fetch_kline.return_value = {
        "symbol": "002131", "period": "W", "source": "local_parquet",
        "bars": [], "truncated": False,
    }
    r = client.get("/api/stock/kline/002131?period=W")
    assert r.status_code == 200
    assert r.json()["period"] == "W"


def test_kline_minute(client, stock_service_stub) -> None:
    stock_service_stub.fetch_kline.return_value = {
        "symbol": "002131", "period": "5m", "source": "akshare_realtime",
        "bars": [], "truncated": False,
    }
    r = client.get("/api/stock/kline/002131?period=5m")
    assert r.status_code == 200
    assert r.json()["source"] == "akshare_realtime"


def test_kline_invalid_period_400(client, stock_service_stub) -> None:
    r = client.get("/api/stock/kline/002131?period=X")
    assert r.status_code == 422  # FastAPI 参数校验 422


def test_kline_akshare_fail_502(client, stock_service_stub) -> None:
    stock_service_stub.fetch_kline.side_effect = RuntimeError("network down")
    r = client.get("/api/stock/kline/002131?period=1m")
    assert r.status_code == 502
    assert "upstream" in r.json()["detail"]


# ============================================================================
# /api/stock/intraday
# ============================================================================


def test_intraday_happy_path(client, stock_service_stub) -> None:
    stock_service_stub.fetch_intraday.return_value = {
        "symbol": "002131", "days": 1,
        "points": [{"t": "2026-07-03 09:30:00", "price": 4.59, "avg": 4.6, "volume": 12000}],
    }
    r = client.get("/api/stock/intraday/002131?days=1")
    assert r.status_code == 200
    d = r.json()
    assert d["days"] == 1
    assert len(d["points"]) == 1


def test_intraday_akshare_fail_502(client, stock_service_stub) -> None:
    stock_service_stub.fetch_intraday.side_effect = RuntimeError("boom")
    r = client.get("/api/stock/intraday/002131?days=5")
    assert r.status_code == 502


# ============================================================================
# /api/stock/analyze
# ============================================================================


def test_analyze_happy_path(client, stock_service_stub, assets) -> None:
    from akq_agents.services.stock_detail import StockOverview

    stock_service_stub.fetch_overview.return_value = StockOverview(
        symbol="002131", name="利欧股份", industry="通用设备",
        industry_pct_change=3.35, market_cap=3.1e10, pe_ratio=-722.48, pb_ratio=1.23,
        listing_date="2011-01-27", quote={"price": 4.59, "pct_change": -0.43},
        as_of="2026-07-03T15:00:00", degraded_fields=[],
    )
    stock_service_stub.fetch_kline.return_value = {
        "symbol": "002131", "period": "D", "source": "local_parquet",
        "bars": [{"t": f"2026-06-{i:02d}", "o": 4.5, "c": 4.5 + i * 0.01, "l": 4.4, "h": 4.6, "v": 10000, "a": 45000.0}
                 for i in range(1, 31)],
        "truncated": False,
    }
    container = assets["container"]
    container.llm_orchestrator.run_analyst.return_value = (
        "## 技术面\n近 30 日均价上行。\n\n## 量价\n成交温和。\n\n## 估值\n数据缺失。\n\n## 风险提示\n注意波动。"
    )
    r = client.post("/api/stock/analyze/002131", json={"period_context": "D"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["symbol"] == "002131"
    assert "## 技术面" in d["content"]
    assert "## 风险提示" in d["content"]
    # disclaimer 尾巴应被拼上
    assert "本报告由 LLM 生成" in d["content"] or "研究参考" in d["content"]
    assert d["session_id"].startswith("stock:002131:")
    # session 里应写了 3 行消息（system + user + assistant）
    msgs = container.llm_store.list_messages(d["session_id"], limit=10)
    roles = [m.role for m in msgs]
    assert roles == ["system", "user", "assistant"]


def test_analyze_llm_gateway_error_502(client, stock_service_stub, assets) -> None:
    from akq_agents.services.llm.client import LLMGatewayError
    from akq_agents.services.stock_detail import StockOverview

    stock_service_stub.fetch_overview.return_value = StockOverview(
        symbol="002131", name="利欧股份", industry=None, industry_pct_change=None,
        market_cap=None, pe_ratio=None, pb_ratio=None, listing_date=None,
        quote={}, as_of="2026-07-03T15:00:00", degraded_fields=[],
    )
    stock_service_stub.fetch_kline.return_value = {
        "symbol": "002131", "period": "D", "source": "local_parquet",
        "bars": [], "truncated": False,
    }
    container = assets["container"]
    container.llm_orchestrator.run_analyst.side_effect = LLMGatewayError(
        "gateway timeout", reason_code="TIMEOUT"
    )
    r = client.post("/api/stock/analyze/002131", json={})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["reason_code"] == "TIMEOUT"


def test_analyze_llm_not_configured_503(client, stock_service_stub, assets) -> None:
    container = assets["container"]
    container.llm_orchestrator = None
    r = client.post("/api/stock/analyze/002131", json={})
    assert r.status_code == 503


# ============================================================================
# /api/stock/search
# ============================================================================


def test_search_by_code(client, stock_service_stub) -> None:
    stock_service_stub.search.return_value = [
        {"symbol": "600519", "name": "贵州茅台"},
        {"symbol": "600000", "name": "浦发银行"},
    ]
    r = client.get("/api/stock/search?q=6&limit=8")
    assert r.status_code == 200
    d = r.json()
    assert d["n"] == 2
    assert d["matches"][0]["symbol"] == "600519"


def test_search_empty_query(client, stock_service_stub) -> None:
    stock_service_stub.search.return_value = []
    r = client.get("/api/stock/search?q=")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_search_limit_capped(client, stock_service_stub) -> None:
    """limit 上界（50）超出返回 422。"""
    r = client.get("/api/stock/search?q=6&limit=999")
    assert r.status_code == 422


# ============================================================================
# /stock/{symbol} 页面
# ============================================================================


def test_stock_page_renders_html(client, stock_service_stub) -> None:
    r = client.get("/stock/002131")
    assert r.status_code == 200
    text = r.text
    assert "<html" in text.lower()
    # 页面里应有 symbol 变量注入
    assert "002131" in text


def test_stock_page_strips_prefix(client, stock_service_stub) -> None:
    """sh600519 前缀应被剥掉再传入模板。"""
    r = client.get("/stock/sh600519")
    assert r.status_code == 200
    assert "600519" in r.text


def test_stock_page_invalid_symbol_redirects(client, stock_service_stub) -> None:
    """无效 symbol 重定向到 /research，而不是 404。"""
    r = client.get("/stock/ABCDEF", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/research"


def test_nav_has_stock_search_input(client) -> None:
    """随便打开一个页面（/research），验证 nav 里有全局搜索框（commit 2 前端）。"""
    r = client.get("/research")
    assert r.status_code == 200
    assert 'id="hotkey-search"' in r.text
    # 联想列表容器
    assert 'id="nav-search-suggest"' in r.text


def test_stock_page_has_ai_panel(client, stock_service_stub) -> None:
    """验证个股页含 AI 综合分析折叠面板 DOM (K 线下方常驻, 不再是右侧抽屉)."""
    r = client.get("/stock/002131")
    assert r.status_code == 200
    text = r.text
    # 折叠面板容器 + head 切换按钮 + 内容/输入区
    assert 'id="ai-card"' in text
    assert 'id="ai-toggle"' in text
    assert 'id="ai-body"' in text
    assert 'id="ai-content"' in text
    assert 'id="ai-textarea"' in text
    assert 'id="ai-send"' in text
    # 旧右侧抽屉 DOM 应彻底移除 (防重构回退)
    assert 'id="ai-analyze-btn"' not in text
    assert 'id="ai-drawer"' not in text
    # 复用 /api/chat 追问
    assert "/api/chat/sessions/" in text


# ============================================================================
# follow-ups
# ============================================================================


def test_research_trade_list_links_to_stock(client) -> None:
    """follow-up 1：/research 页 renderRow 里 symbol 应包成 <a href="/stock/${...}"> 链接。"""
    r = client.get("/research")
    assert r.status_code == 200
    # renderRow 是 JS 模板字符串，检查其中的链接结构
    assert 'href="/stock/${it.symbol}"' in r.text
    # 归因表也有 r.symbol 版本
    assert 'href="/stock/${r.symbol}"' in r.text
    # holdings 表 h.symbol 版本
    assert 'href="/stock/${h.symbol}"' in r.text
    # 复用 stock-link 样式
    assert 'class="stock-link"' in r.text


def test_stock_page_has_chart_hint_container(client, stock_service_stub) -> None:
    """follow-up 2：个股页有 chart-hint 容器，用于分钟 K 未复权提示。"""
    r = client.get("/stock/002131")
    assert 'id="chart-hint"' in r.text
    # JS 逻辑：source === 'akshare_realtime' 时显示未复权提示
    assert "akshare_realtime" in r.text
    assert "不复权" in r.text


def test_chat_list_sessions_filters_stock_prefix(client, assets) -> None:
    """follow-up 3：/api/chat/sessions 不返回 stock:* 前缀的会话。"""
    container = assets["container"]
    # 造几个 session（stock:* 应被过滤，chat:* 保留）
    container.llm_store.append_message(session_id="stock:002131:aaaa", role="user", content="test")
    container.llm_store.append_message(session_id="chat:normal01", role="user", content="hi")
    container.llm_store.append_message(session_id="stock:600519:bbbb", role="user", content="test")
    r = client.get("/api/chat/sessions")
    assert r.status_code == 200
    ids = [s["session_id"] for s in r.json()["sessions"]]
    assert "chat:normal01" in ids
    assert not any(sid.startswith("stock:") for sid in ids)


def test_chat_page_renders_without_stock_sessions(client, assets) -> None:
    """follow-up 3：/chat 页面渲染时 sessions 列表应过滤 stock:* 前缀。"""
    container = assets["container"]
    container.llm_store.append_message(session_id="stock:002131:aaaa", role="user", content="test")
    container.llm_store.append_message(session_id="chat:visible01", role="user", content="hi")
    r = client.get("/chat")
    assert r.status_code == 200
    # sessions 列表通过 jinja for 循环渲染 session_id 到 data-session-id 属性
    assert 'data-session-id="chat:visible01"' in r.text
    assert 'data-session-id="stock:002131:aaaa"' not in r.text
