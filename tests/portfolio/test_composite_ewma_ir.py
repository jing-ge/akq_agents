"""composite._ewma_abs_ir 行为单测。"""
from __future__ import annotations

from types import SimpleNamespace

from akq_agents.services.portfolio.composite import CompositeScorer


def _m(ir: float | None) -> SimpleNamespace:
    """构造 factor_metric 替身（只有 .ir 字段）。"""
    return SimpleNamespace(ir=ir)


def test_ewma_abs_ir_returns_none_for_empty() -> None:
    assert CompositeScorer._ewma_abs_ir([]) is None


def test_ewma_abs_ir_returns_none_when_all_ir_none() -> None:
    """全是 None 的 ir 视为无效。"""
    assert CompositeScorer._ewma_abs_ir([_m(None), _m(None)]) is None


def test_ewma_abs_ir_clamps_negative_ir_to_zero() -> None:
    """R4: 长期 IR 为负 (反向预测能力) 时应当被截断到 0,
    与 fallback 路径 ``max(float(m.ir), 0.0)`` 语义一致。
    之前用 abs(ir) 会把负 IR 当成"还不错的因子"使用 → 组合朝错误方向倾斜。"""
    # 全负 IR (history DESC, 最新在前)
    history = [_m(-0.2), _m(-0.15), _m(-0.18)]
    result = CompositeScorer._ewma_abs_ir(history)
    assert result == 0.0, f"全负 IR 应返回 0.0，实际 {result}"


def test_ewma_abs_ir_positive_values_unchanged() -> None:
    """正 IR 不受影响，按 EWMA 加权平均。"""
    # 全正 IR
    history = [_m(0.2), _m(0.2), _m(0.2)]
    result = CompositeScorer._ewma_abs_ir(history)
    assert result is not None
    assert abs(result - 0.2) < 1e-9, f"全 0.2 EWMA 应该约 0.2，实际 {result}"


def test_ewma_abs_ir_mixed_signs_negative_truncated() -> None:
    """正负混合时，负 IR 截断到 0 → 拉低 EWMA 均值。"""
    # 最新 +0.3, 之前 -0.3
    history = [_m(0.3), _m(-0.3)]
    result = CompositeScorer._ewma_abs_ir(history)
    # 截断后是 [0.3, 0.0]，EWMA 偏重最新 → 应该接近 0.3 但 < 0.3
    assert result is not None
    assert 0.0 < result < 0.3, f"混合符号 EWMA 应在 (0, 0.3) 之间，实际 {result}"
    # 如果错误地用 abs，会得到约 0.3（两个都按 0.3 算）
    assert abs(result - 0.3) > 0.05, "似乎仍在用 abs(ir) — 应该截负到 0"
