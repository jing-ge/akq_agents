"""StockNameStore 基本读写测试。"""
from __future__ import annotations

from pathlib import Path

from akq_agents.services.data.stock_names import StockNameStore


def test_upsert_and_load(tmp_path: Path) -> None:
    db = tmp_path / "meta.db"
    store = StockNameStore(db)

    # 空表
    assert store.load_all() == {}

    # 批量写入
    n = store.upsert_many({"600519": "贵州茅台", "000001": "平安银行"})
    assert n == 2
    out = store.load_all()
    assert out["600519"] == "贵州茅台"
    assert out["000001"] == "平安银行"

    # 覆盖已存在的
    store.upsert_many({"600519": "贵州茅台ST"})
    assert store.load_all()["600519"] == "贵州茅台ST"


def test_upsert_skips_empty_name(tmp_path: Path) -> None:
    """空 name 不应写入。"""
    store = StockNameStore(tmp_path / "meta.db")
    store.upsert_many({"600001": "", "600002": "X"})
    out = store.load_all()
    assert "600001" not in out
    assert out["600002"] == "X"


def test_upsert_many_empty_is_noop(tmp_path: Path) -> None:
    store = StockNameStore(tmp_path / "meta.db")
    assert store.upsert_many({}) == 0
    assert store.load_all() == {}
