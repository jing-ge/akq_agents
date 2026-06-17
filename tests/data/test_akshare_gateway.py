from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from akq_agents.services.data.exceptions import FetchError


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
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


def test_fetch_spot_maps_columns(gateway_cls: Any, gateway_config: Any) -> None:
    fake_ak = SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            {
                "代码": ["000001"],
                "名称": ["平安银行"],
                "最新价": [10.5],
                "成交量": [1000],
                "成交额": [2000.0],
                "换手率": [1.2],
            }
        )
    )

    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    result = gateway.fetch_spot()

    assert list(result.columns) == [
        "symbol",
        "name",
        "price",
        "volume",
        "amount",
        "turnover_ratio",
    ]
    assert result.iloc[0].to_dict() == {
        "symbol": "000001",
        "name": "平安银行",
        "price": 10.5,
        "volume": 1000,
        "amount": 2000.0,
        "turnover_ratio": 1.2,
    }


def test_fetch_spot_schema_drift_raises(gateway_cls: Any, gateway_config: Any) -> None:
    fake_ak = SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            {
                "名称": ["平安银行"],
                "最新价": [10.5],
                "成交量": [1000],
                "成交额": [2000.0],
                "换手率": [1.2],
            }
        )
    )

    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    with pytest.raises(FetchError) as exc_info:
        gateway.fetch_spot()

    assert exc_info.value.reason_code == "SCHEMA_DRIFT"
    assert "代码" in exc_info.value.message


def test_fetch_ohlcv_maps_columns_and_formats_dates(gateway_cls: Any, gateway_config: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_hist(**kwargs: Any) -> pd.DataFrame:
        calls.append(kwargs)
        return pd.DataFrame(
            {
                "日期": ["2026-06-16"],
                "开盘": [10.0],
                "收盘": [10.5],
                "最高": [10.6],
                "最低": [9.9],
                "成交量": [1000],
                "成交额": [2000.0],
            }
        )

    fake_ak = SimpleNamespace(stock_zh_a_hist=fake_hist)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    result = gateway.fetch_ohlcv("000001", date(2026, 6, 1), date(2026, 6, 16))

    assert calls == [
        {
            "symbol": "000001",
            "period": "daily",
            "adjust": "qfq",
            "start_date": "20260601",
            "end_date": "20260616",
        }
    ]
    assert list(result.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert result.iloc[0].to_dict() == {
        "date": pd.Timestamp("2026-06-16"),
        "open": 10.0,
        "high": 10.6,
        "low": 9.9,
        "close": 10.5,
        "volume": 1000,
        "amount": 2000.0,
    }


def test_fetch_trading_dates_parses_strings_and_timestamps(gateway_cls: Any, gateway_config: Any) -> None:
    fake_ak = SimpleNamespace(
        tool_trade_date_hist_sina=lambda: pd.DataFrame(
            {"trade_date": ["2026-06-16", pd.Timestamp("2026-06-17")]}
        )
    )
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    result = gateway.fetch_trading_dates()

    assert result == [date(2026, 6, 16), date(2026, 6, 17)]


def test_throttle_sleeps_on_second_fetch(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    clock = MonotonicClock([10.0, 10.1])
    fake_ak = SimpleNamespace(stock_zh_a_spot_em=lambda: pd.DataFrame({"代码": [], "名称": [], "最新价": [], "成交量": [], "成交额": [], "换手率": []}))
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)

    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.monotonic", clock)

    gateway.fetch_spot()
    gateway.fetch_spot()

    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(0.4, abs=1e-6)


def test_fetch_spot_retries_network_error_then_succeeds(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"count": 0}
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000

    def fake_spot() -> pd.DataFrame:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("connection reset")
        return pd.DataFrame(
            {
                "代码": ["000001"],
                "名称": ["平安银行"],
                "最新价": [10.5],
                "成交量": [1000],
                "成交额": [2000.0],
                "换手率": [1.2],
            }
        )

    fake_ak = SimpleNamespace(stock_zh_a_spot_em=fake_spot)
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    result = gateway.fetch_spot()

    assert attempts["count"] == 3
    assert result.iloc[0]["symbol"] == "000001"
    assert sleep.calls == [0.5, 1.0]


def test_fetch_spot_retry_exhausted_raises_network(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000
    fake_ak = SimpleNamespace(stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(ConnectionError("connection failed")))
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc_info:
        gateway.fetch_spot()

    assert exc_info.value.reason_code == "NETWORK"
    assert sleep.calls == [0.5, 1.0, 2.0]


def test_fetch_spot_retry_exhausted_raises_rate_limited(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    gateway_config.qps = 1_000_000
    fake_ak = SimpleNamespace(stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(RuntimeError("429 rate limit exceeded")))
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc_info:
        gateway.fetch_spot()

    assert exc_info.value.reason_code == "RATE_LIMITED"
    assert sleep.calls == [0.5, 1.0, 2.0]


def test_fetch_spot_unknown_error_does_not_retry(
    gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep = SleepRecorder()
    fake_ak = SimpleNamespace(stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(ValueError("bad payload")))
    gateway = gateway_cls(gateway_config, ak_module=fake_ak)
    monkeypatch.setattr("akq_agents.services.data.akshare_gateway.time.sleep", sleep)

    with pytest.raises(FetchError) as exc_info:
        gateway.fetch_spot()

    assert exc_info.value.reason_code == "UNKNOWN"
    assert sleep.calls == []


def test_fetch_methods_raise_when_akshare_not_installed(gateway_cls: Any, gateway_config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = __import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "akshare":
            raise ImportError("No module named akshare")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    gateway = gateway_cls(gateway_config, ak_module=None)

    with pytest.raises(FetchError) as exc_info:
        gateway.fetch_spot()

    assert exc_info.value.reason_code == "UNKNOWN"
    assert "not installed" in exc_info.value.message
