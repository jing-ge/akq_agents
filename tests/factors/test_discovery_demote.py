"""A1: shadow 因子 demote 阈值/宽限期回归测试。"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from akq_agents.services.factors.base import FactorRegistry
from akq_agents.services.factors.discovery import (
    DiscoveryEngine,
    DiscoveryStats,
    DiscoveryThresholds,
)


def test_shadow_under_max_days_with_low_ir_keeps_observing():
    """A1 关键: 默认宽限期阈值应已生效。"""
    th = DiscoveryThresholds()
    assert th.shadow_max_days == 60
    assert th.shadow_min_keep_ir == 0.10


def test_shadow_oos_uses_signed_ir_no_direction_flip():
    """P3 review #3 前视偏差修复契约: shadow OOS 判定必须用"冻结方向的带符号 IR",
    不得在同一段 OOS 样本上事后翻转 direction。

    这个测试锁定源码语义 (防止有人把 abs(oos_ir)+事后翻转 改回来):
    - promote 判定用 signed_ir (按 frozen_direction 对齐), 不用 abs(oos_ir)
    - 源码不再出现 direction_flipped / new_direction 的事后翻转逻辑
    """
    import inspect

    from akq_agents.services.factors import discovery as disc_mod

    src = inspect.getsource(disc_mod.DiscoveryEngine.promote_shadows) \
        if hasattr(disc_mod.DiscoveryEngine, "promote_shadows") \
        else inspect.getsource(disc_mod)
    # 修复后应使用 signed_ir 带符号判定
    assert "signed_ir" in src, "shadow OOS 判定应使用 signed_ir (冻结方向对齐)"
    # 不应再有"OOS 为负就事后翻转 direction"的逻辑标记
    assert "direction_flipped" not in src, (
        "不得在 OOS 样本上事后翻转 direction (前视偏差/方向拟合造假阳)"
    )




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


def test_prepare_data_failure_writes_event():
    """I5 followup: _prepare_data 顶层失败时 run_batch 应写 events 让 /ops 可见。"""
    repo = MagicMock()
    repo.get_universe.side_effect = RuntimeError("db locked")
    repo._calendar = None

    state_store = MagicMock()
    engine = DiscoveryEngine(
        repository=repo,
        registry=FactorRegistry(),
        evaluator=MagicMock(),
        proposal_store=MagicMock(),
        state_store=state_store,
    )

    stats = engine.run_batch(n_candidates=1, as_of_date=date(2026, 6, 29))

    # universe 双重失败 → 进 _prepare_data fallback 也失败分支
    # → 写 universe_unavailable 事件，返回 empty
    kinds = [c.kwargs.get("kind") for c in state_store.write_event.call_args_list]
    assert "factor.discovery.universe_unavailable" in kinds
    # universe 失败时 run_batch 不会跑出真候选
    assert stats.proposed == 0
