"""B1 regression: factor.discovery 用 hour 桶 partition 避免一天只跑一次。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from akq_agents.orchestrator.jobs.factor_discovery import _partition_for_now, run_once_now


def test_partition_is_hour_bucket() -> None:
    """partition 必须含小时（不只是日期），否则一天只能跑一次。"""
    p = _partition_for_now()
    # 格式 YYYY-MM-DDTHH (13 字符)
    assert len(p) == 13
    assert p[10] == "T"
    # 第 11/12 位是小时
    assert p[11:].isdigit()


def test_partition_changes_each_hour() -> None:
    """不同小时调用，partition 必须不同（不会被 JobRunner 幂等吞掉）。"""
    with patch("akq_agents.orchestrator.jobs.factor_discovery.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 25, 14, 0, 0)
        mock_dt.strftime = datetime.strftime
        p1 = _partition_for_now()
        mock_dt.now.return_value = datetime(2026, 6, 25, 16, 0, 0)
        p2 = _partition_for_now()
    assert p1 != p2
    assert p1.endswith("14")
    assert p2.endswith("16")


def test_run_once_now_uses_hour_partition() -> None:
    """run_once_now (web/CLI 手动触发) 也走 hour 桶，让用户在同一天可以多次触发看新候选。"""
    runner = MagicMock()
    runner.run.return_value = MagicMock(status="ok")
    services = {"discovery_engine": MagicMock()}
    services["discovery_engine"].run_batch.return_value = MagicMock(as_dict=lambda: {"ok": True})

    run_once_now(runner, services, n_candidates=10)

    # 第一个位置参数 job_id, 第二个 partition
    args, _kwargs = runner.run.call_args
    job_id, partition = args[0], args[1]
    assert job_id == "factor.discovery"
    assert len(partition) == 13  # hour 桶
    assert partition[10] == "T"
