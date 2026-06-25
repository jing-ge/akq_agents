"""research.py 的 _backfill_names helper 单测。

确保 portfolio_snapshots 的 name 列为空时，stock_name_store 能兜底补名称。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from akq_agents.web.api.research import _backfill_names


def _svc_with_store(name_map: dict[str, str] | None) -> SimpleNamespace:
    """构造一个 ServiceContainer 替身：workflow.services.get('stock_name_store') 行为可控。"""
    store = MagicMock()
    if name_map is None:
        store = None
    else:
        store.load_all.return_value = name_map
    services = {"stock_name_store": store} if store is not None else {}
    return SimpleNamespace(workflow=SimpleNamespace(services=services))


def test_backfill_names_fills_empty() -> None:
    """name 空时应从 store 补。"""
    items = [
        {"symbol": "600519", "name": ""},
        {"symbol": "000001", "name": None},
    ]
    svc = _svc_with_store({"600519": "贵州茅台", "000001": "平安银行"})
    _backfill_names(svc, items)
    assert items[0]["name"] == "贵州茅台"
    assert items[1]["name"] == "平安银行"


def test_backfill_names_preserves_existing() -> None:
    """已有 name 不应覆盖。"""
    items = [{"symbol": "600519", "name": "原始名"}]
    svc = _svc_with_store({"600519": "贵州茅台"})
    _backfill_names(svc, items)
    assert items[0]["name"] == "原始名"


def test_backfill_names_no_store_is_noop() -> None:
    """store 缺失时不抛错、不改 items。"""
    items = [{"symbol": "600519", "name": ""}]
    svc = _svc_with_store(None)
    _backfill_names(svc, items)
    assert items[0]["name"] == ""


def test_backfill_names_empty_items_is_noop() -> None:
    """空 items 不应碰 store（避免不必要 IO）。"""
    svc = _svc_with_store({"600519": "X"})
    _backfill_names(svc, [])
    # store.load_all 不应被调
    svc.workflow.services["stock_name_store"].load_all.assert_not_called()


def test_backfill_names_skips_store_call_when_all_filled() -> None:
    """所有 items 都已有 name 时，不应触发 load_all。"""
    items = [{"symbol": "600519", "name": "贵州茅台"}]
    svc = _svc_with_store({"600519": "X"})
    _backfill_names(svc, items)
    svc.workflow.services["stock_name_store"].load_all.assert_not_called()
    assert items[0]["name"] == "贵州茅台"


def test_backfill_names_missing_in_store_keeps_empty() -> None:
    """store 里没记录的 symbol，保持空字符串。"""
    items = [{"symbol": "999999", "name": ""}]
    svc = _svc_with_store({"600519": "贵州茅台"})
    _backfill_names(svc, items)
    assert items[0]["name"] == ""
