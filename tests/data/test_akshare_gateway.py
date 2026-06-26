"""``AKShareGateway`` 单元测试。

测试覆盖新浪 + 交易所官方源接口，注入 fake ak module，**绝不发起真实网络
请求**。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from akq_agents.services.data.exceptions import FetchError


class SleepRecorder:
    """记录 time.sleep 调用; 自动过滤亚毫秒级 throttle 噪音。

    qps 极大 (如测试里 1e6) 时 _throttle 偶尔会算出 ~1µs 的 sleep_for 也调 time.sleep,
    污染断言。测试关心的是 retry 的 backoff sleep (0.5s/1.0s/2.0s), 不是 rate limiter,
    所以 < 1ms 视为噪音直接忽略。
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        if seconds >= 0.001:
            self.calls.append(seconds)


class MonotonicClock:
    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._index = 0

    def __call__(self) -> float:
        if self._index >= len(self._values):
            return self._values[-1]
        value = self._values[self._index]
        self._index += 1
        return value


@pytest.fixture
def gateway_config() -> Any:
    from akq_agents.models.data_config import AkshareGatewayConfig

    return AkshareGatewayConfig(qps=2.0, max_retries=3, timeout_s=30, backoff_base_s=0.5)


@pytest.fixture
def gateway_cls() -> Any:
    from akq_agents.services.data.akshare_gateway import AKShareGateway

    return AKShareGateway


def _make_sh_df(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "证券代码": [r[0] for r in rows],
            "证券简称": [r[1] for r in rows],
            "证券全称": ["x"] * len(rows),
            "公司简称": ["y"] * len(rows),
            "公司全称": ["z"] * len(rows),
            "上市日期": [r[2] for r in rows],
        }
    )


def _make_sz_df(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "板块": ["主板"] * len(rows),
            "A股代码": [r[0] for r in rows],
            "A股简称": [r[1] for r in rows],
            "A股上市日期": [r[2] for r in rows],
            "A股总股本": ["1"] * len(rows),
            "A股流通股本": ["1"] * len(rows),
            "所属行业": ["其它"] * len(rows),
        }
    )


# ----------------------------------------------------------------- fetch_spot

def test_fetch_spot_merges_sh_and_sz(gateway_cls: Any, gateway_config: Any) -> None:
    fake_ak = SimpleNamespace(
        stock_info_sh_name_code=lambda: _make_sh_df([("600519", "贵州茅台", "2001-08-27")]),
        stock_info_sz_name_code=lambda: _make_sz_df([("000001", "平安银行", "1991-04-03")]),
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    df = gateway.fetch_spot()
    assert list(df.columns) == ["symbol", "name", "listing_date"]
    assert len(df) == 2
    syms = df["symbol"].tolist()
    assert "600519" in syms
    assert "000001" in syms
    row = df[df["symbol"] == "600519"].iloc[0]
    assert row["name"] == "贵州茅台"
    assert row["listing_date"] == date(2001, 8, 27)


def test_fetch_spot_drops_duplicates(gateway_cls: Any, gateway_config: Any) -> None:
    # 同一 symbol 同时出现在 SH 和 SZ → 保留先出现的（SH）
    fake_ak = SimpleNamespace(
        stock_info_sh_name_code=lambda: _make_sh_df([("600519", "SH名", "2001-08-27")]),
        stock_info_sz_name_code=lambda: _make_sz_df([("600519", "SZ名", "2099-01-01")]),
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    df = gateway.fetch_spot()
    assert len(df) == 1
    assert df.iloc[0]["name"] == "SH名"


def test_fetch_spot_schema_drift_when_sh_missing_column(
    gateway_cls: Any, gateway_config: Any
) -> None:
    bad_sh = pd.DataFrame({"证券简称": ["x"], "上市日期": ["2001-08-27"]})  # 缺 "证券代码"
    fake_ak = SimpleNamespace(
        stock_info_sh_name_code=lambda: bad_sh,
        stock_info_sz_name_code=lambda: _make_sz_df([("000001", "平安", "1991-04-03")]),
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    with pytest.raises(FetchError) as exc:
        gateway.fetch_spot()
    assert exc.value.reason_code == "SCHEMA_DRIFT"
    assert "证券代码" in exc.value.message


# ----------------------------------------------------------------- fetch_ohlcv

def test_fetch_ohlcv_uses_sina_with_market_prefix(
    gateway_cls: Any, gateway_config: Any
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_daily(**kwargs: Any) -> pd.DataFrame:
        calls.append(kwargs)
        return pd.DataFrame(
            {
                "date": ["2026-06-16"],
                "open": [10.0],
                "high": [10.6],
                "low": [9.9],
                "close": [10.5],
                "volume": [1000.0],
                "amount": [2000.0],
                "outstanding_share": [1e9],
                "turnover": [0.001],
            }
        )

    fake_ak = SimpleNamespace(stock_zh_a_daily=fake_daily)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    df = gateway.fetch_ohlcv("600519", date(2026, 6, 1), date(2026, 6, 16))

    # 验证带前缀 + 日期格式
    assert calls == [
        {
            "symbol": "sh600519",
            "start_date": "20260601",
            "end_date": "20260616",
            "adjust": "qfq",
        }
    ]
    # 验证标准化列（额外字段 outstanding_share/turnover 被丢弃）
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert df.iloc[0]["date"] == pd.Timestamp("2026-06-16")
    assert df.iloc[0]["close"] == 10.5


def test_fetch_ohlcv_prefix_for_sz_symbol(gateway_cls: Any, gateway_config: Any) -> None:
    calls: list[dict[str, Any]] = []
    fake_ak = SimpleNamespace(
        stock_zh_a_daily=lambda **kw: calls.append(kw)
        or pd.DataFrame(
            {
                "date": ["2026-06-16"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
                "amount": [1.0],
            }
        ),
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    gateway.fetch_ohlcv("000001", date(2026, 6, 16), date(2026, 6, 16))
    assert calls[0]["symbol"] == "sz000001"


def test_fetch_ohlcv_schema_drift_when_missing_close(
    gateway_cls: Any, gateway_config: Any
) -> None:
    fake_ak = SimpleNamespace(
        stock_zh_a_daily=lambda **kw: pd.DataFrame(
            {"date": ["2026-06-16"], "open": [10.0]}  # 缺 close 等核心列
        )
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    with pytest.raises(FetchError) as exc:
        gateway.fetch_ohlcv("600519", date(2026, 6, 16), date(2026, 6, 16))
    assert exc.value.reason_code == "SCHEMA_DRIFT"


def test_with_market_prefix_handles_all_segments() -> None:
    from akq_agents.services.data.akshare_gateway import _with_market_prefix

    assert _with_market_prefix("600519") == "sh600519"
    assert _with_market_prefix("000001") == "sz000001"
    assert _with_market_prefix("300750") == "sz300750"
    assert _with_market_prefix("832000") == "bj832000"

    for bad in ("", "abc", "X12345"):
        with pytest.raises(ValueError):
            _with_market_prefix(bad)

    with pytest.raises(ValueError):
        _with_market_prefix("100000")  # 1 开头不在已知 A 股市场


# ----------------------------------------------------------------- stubs

def test_fetch_st_list_returns_empty_stub_with_warning(
    gateway_cls: Any, gateway_config: Any
) -> None:
    gateway = gateway_cls(gateway_config, ak_module=SimpleNamespace())
    with pytest.warns(RuntimeWarning):
        result = gateway.fetch_st_list()
    assert result == []


def test_fetch_individual_info_returns_stub(gateway_cls: Any, gateway_config: Any) -> None:
    gateway = gateway_cls(gateway_config, ak_module=SimpleNamespace())
    info = gateway.fetch_individual_info("600519")
    assert info == {"listing_date": None, "is_suspended": None}


# ----------------------------------------------------------------- trading dates

def test_fetch_trading_dates_parses_strings_and_timestamps(
    gateway_cls: Any, gateway_config: Any
) -> None:
    fake_ak = SimpleNamespace(
        tool_trade_date_hist_sina=lambda: pd.DataFrame(
            {"trade_date": ["2026-06-16", pd.Timestamp("2026-06-17")]}
        )
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    result = gateway.fetch_trading_dates()
    assert result == [date(2026, 6, 16), date(2026, 6, 17)]


# ----------------------------------------------------------------- throttle + retry

def test_throttle_sleeps_on_second_fetch(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    clock = MonotonicClock([10.0, 10.1])
    fake_ak = SimpleNamespace(
        stock_info_sh_name_code=lambda: _make_sh_df([]),
        stock_info_sz_name_code=lambda: _make_sz_df([]),
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.monotonic", clock)

    gateway.fetch_spot()  # qps=2 → 间隔 0.5s；两次 sh+sz 共 2 次 _call
    # fetch_spot 内部触发两次 _call (sh, sz)，sleep 触发取决于 clock 推进
    # 至少调用了一次 sleep
    assert len(sleep.calls) >= 1


def test_fetch_retries_network_error_then_succeeds(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"count": 0}
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000

    def fake_daily(**kw: Any) -> pd.DataFrame:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("connection reset")
        return pd.DataFrame(
            {
                "date": ["2026-06-16"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
                "amount": [1.0],
            }
        )

    fake_ak = SimpleNamespace(stock_zh_a_daily=fake_daily)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    result = gateway.fetch_ohlcv("600519", date(2026, 6, 16), date(2026, 6, 16))
    assert attempts["count"] == 3
    assert result.iloc[0]["close"] == 1.0
    assert sleep.calls == [0.5, 1.0]


def test_retry_exhausted_network(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000

    def always_fail(**kw: Any) -> pd.DataFrame:
        raise ConnectionError("connection failed")

    fake_ak = SimpleNamespace(stock_zh_a_daily=always_fail)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc:
        gateway.fetch_ohlcv("600519", date(2026, 6, 16), date(2026, 6, 16))
    assert exc.value.reason_code == "NETWORK"
    assert sleep.calls == [0.5, 1.0, 2.0]


def test_retry_exhausted_rate_limited(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000

    def always_429(**kw: Any) -> pd.DataFrame:
        raise RuntimeError("429 rate limit exceeded")

    fake_ak = SimpleNamespace(stock_zh_a_daily=always_429)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc:
        gateway.fetch_ohlcv("600519", date(2026, 6, 16), date(2026, 6, 16))
    assert exc.value.reason_code == "RATE_LIMITED"


def test_unknown_error_does_not_retry(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()

    def bad_payload(**kw: Any) -> pd.DataFrame:
        raise ValueError("bad payload")

    fake_ak = SimpleNamespace(stock_zh_a_daily=bad_payload)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc:
        gateway.fetch_ohlcv("600519", date(2026, 6, 16), date(2026, 6, 16))
    assert exc.value.reason_code == "UNKNOWN"
    assert sleep.calls == []


def test_fetch_methods_raise_when_akshare_not_installed(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = __import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "akshare":
            raise ImportError("No module named akshare")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    gateway = gateway_cls(gateway_config, ak_module=None)
    with pytest.raises(FetchError) as exc:
        gateway.fetch_spot()
    assert exc.value.reason_code == "UNKNOWN"
    assert "not installed" in exc.value.message
