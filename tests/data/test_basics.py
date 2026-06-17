"""批次 0 smoke test：confirm 基础 schema/config/exceptions 可 import 且字段就位。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from akq_agents.models.data_config import DataConfig
from akq_agents.services.data import (
    DataHealth,
    DataNotReady,
    FetchError,
    OHLCVBar,
    QualityCheckFailed,
    RefreshResult,
    UniverseSnapshot,
)


def test_fetch_error_reason_code_and_str() -> None:
    err = FetchError(reason_code="RATE_LIMITED", message="429 too many", symbol="600519")
    assert err.reason_code == "RATE_LIMITED"
    assert err.symbol == "600519"
    assert "RATE_LIMITED" in str(err)
    assert "600519" in str(err)


def test_data_not_ready_carries_missing() -> None:
    err = DataNotReady({"600519": [date(2026, 6, 17)]})
    assert err.missing["600519"] == [date(2026, 6, 17)]


def test_quality_check_failed_collects_failed_names() -> None:
    err = QualityCheckFailed({"row_count": False, "null_rate": True})
    assert "row_count" in str(err)


def test_ohlcv_bar_roundtrip() -> None:
    bar = OHLCVBar(
        symbol="600519",
        date=date(2026, 6, 17),
        open=1700.0,
        high=1720.0,
        low=1690.0,
        close=1710.0,
        volume=100000,
        amount=171_000_000,
        turnover=0.005,
    )
    assert bar.close == 1710.0
    assert bar.turnover == 0.005


def test_universe_snapshot_defaults() -> None:
    snap = UniverseSnapshot(date=date(2026, 6, 17), symbols=["600519", "000001"])
    assert snap.excluded == {}


def test_data_health_defaults_failed() -> None:
    h = DataHealth()
    assert h.health == "FAILED"
    assert h.universe_size_today == 0


def test_refresh_result_minimum() -> None:
    r = RefreshResult(target_date=date(2026, 6, 17))
    assert r.fetched == 0
    assert r.skipped_non_trading_day is False


def test_data_config_loads_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
data:
  base_dir: ./datax
  akshare:
    qps: 3
  quality:
    min_universe_size: 100
""".strip()
    yaml_path = tmp_path / "data.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    cfg = DataConfig.from_yaml(yaml_path)
    assert cfg.base_dir == "./datax"
    assert cfg.akshare.qps == 3
    assert cfg.quality.min_universe_size == 100
    # Defaults remain intact for unspecified blocks.
    assert cfg.universe.min_listing_days == 180


def test_data_config_resolves_relative_base_dir(tmp_path: Path) -> None:
    cfg = DataConfig(base_dir="./mydata")
    resolved = cfg.resolve_base_dir(tmp_path)
    assert resolved == (tmp_path / "mydata").resolve()


def test_data_config_keeps_absolute_base_dir(tmp_path: Path) -> None:
    abs_path = tmp_path / "abs"
    cfg = DataConfig(base_dir=str(abs_path))
    assert cfg.resolve_base_dir(Path("/anywhere/else")) == abs_path
