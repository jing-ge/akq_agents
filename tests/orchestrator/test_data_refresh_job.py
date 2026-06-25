"""data.refresh_daily 行为回归测试。"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from akq_agents.orchestrator.jobs.data_refresh import _do
from akq_agents.services.data.exceptions import QualityCheckFailed
from akq_agents.services.data.schemas import RefreshResult


def _mk_repo(refresh_result: RefreshResult, cached_rows: int | None = None) -> MagicMock:
    """构造一个 repo mock：cached 命中 / refresh_daily_fast 返回指定 result。"""
    repo = MagicMock()
    repo.refresh_daily_fast.return_value = refresh_result
    repo._refresh_state_rows.return_value = cached_rows  # None=未命中
    return repo


def test_do_returns_payload_when_quality_passed() -> None:
    repo = _mk_repo(
        RefreshResult(
            target_date=date(2026, 6, 25),
            requested=100, fetched=100, cached_hit=0, failed=0,
            quality_passed=True, duration_s=1.0,
        ),
    )
    out = _do({"data_repository": repo})
    assert out["skipped"] is False
    assert out["quality_passed"] is True
    assert out["fetched"] == 100


def test_do_raises_quality_check_failed_when_quality_passed_is_false() -> None:
    """B-P0-1: quality_passed=False 必须 raise，否则 JobRunner 会当 ok 入库，
    alerter 看不到 + 后续 retry cron 被幂等吞掉。"""
    repo = _mk_repo(
        RefreshResult(
            target_date=date(2026, 6, 25),
            requested=0, fetched=0, cached_hit=0, failed=1,
            quality_passed=False, duration_s=0.5,
        ),
    )
    with pytest.raises(QualityCheckFailed):
        _do({"data_repository": repo})


def test_do_skips_non_trading_day_without_raising() -> None:
    """非交易日 quality_passed=False 但 skipped_non_trading_day=True 不应 raise
    （这是合法 skip，不是数据失败）。"""
    result = RefreshResult(
        target_date=date(2026, 6, 27),  # 周六
        requested=0, fetched=0, cached_hit=0, failed=0,
        skipped_non_trading_day=True,
        quality_passed=False, duration_s=0.01,
    )
    repo = _mk_repo(result)
    out = _do({"data_repository": repo})  # 不抛
    assert out["quality_passed"] is False


def test_do_uses_cache_when_already_fetched_today() -> None:
    """已命中缓存时不调 refresh_daily_fast，直接返回 skipped。"""
    repo = _mk_repo(
        RefreshResult(
            target_date=date(2026, 6, 25),
            requested=100, fetched=100, cached_hit=0, failed=0,
            quality_passed=True, duration_s=1.0,
        ),
        cached_rows=5000,
    )
    out = _do({"data_repository": repo})
    assert out["skipped"] is True
    assert out["cached_rows"] == 5000
    repo.refresh_daily_fast.assert_not_called()
