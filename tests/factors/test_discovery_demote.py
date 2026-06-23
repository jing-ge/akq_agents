"""A1: shadow 因子 demote 阈值/宽限期回归测试。"""
from __future__ import annotations

from akq_agents.services.factors.discovery import DiscoveryStats, DiscoveryThresholds


def test_shadow_under_max_days_with_low_ir_keeps_observing():
    """A1 关键: 默认宽限期阈值应已生效。"""
    th = DiscoveryThresholds()
    assert th.shadow_max_days == 60
    assert th.shadow_min_keep_ir == 0.10



def test_discovery_stats_has_promoted_and_demoted():
    """DiscoveryStats 应该有 promoted / demoted 字段。"""
    stats = DiscoveryStats()
    assert hasattr(stats, "promoted")
    assert hasattr(stats, "demoted")
    assert stats.promoted == 0
    assert stats.demoted == 0
    d = stats.as_dict()
    assert "promoted" in d
    assert "demoted" in d
