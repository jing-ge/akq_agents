"""LLM-based factor brainstorming.

让大模型看现状 → 提出新 recipe → 入库为 llm_suggested。
**不**直接进 OOS 流程；需人工审核。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from akq_agents.services.factors.discovery import _BASES, _OPS, _WINDOWS, _DIRECTIONS
from akq_agents.services.factors.proposal_store import FactorProposalStore

logger = logging.getLogger(__name__)


def build_state_summary(
    registry: Any,
    evaluator: Any,
    store: FactorProposalStore,
) -> str:
    """组装给 LLM 看的 markdown 现状摘要。

    包含：
    - DSL 能力圈（base/op/window/direction 笛卡尔积约束）
    - 已上线因子 (accepted) 的 IC/IR 表现
    - 拒绝率统计：按 (base, op) 二维聚合 rejection rate
    - 最近 20 个 shadow / rejected 的 reason（让 LLM 知道避开什么）
    """
    lines: list[str] = []

    # 1) DSL 能力圈
    lines.append("# 因子构建能力圈（你能用的 DSL）")
    lines.append("")
    lines.append(f"- base 列：{', '.join(sorted(_BASES.keys()))}")
    lines.append(f"- op 算子：{', '.join(_OPS)}")
    lines.append(f"- window 窗口（天）：{', '.join(str(w) for w in _WINDOWS)}")
    lines.append(f"- direction：{', '.join(_DIRECTIONS)}")
    lines.append("")
    lines.append("**recipe 格式**：`{base, op, window, direction}` 四元组。")
    lines.append("**不允许** 新 op / 新 base / 新窗口。")
    lines.append("")

    # 2) 当前已上线因子（registry）
    lines.append("# 当前已上线因子（registry）")
    lines.append("")
    lines.append("| name | direction | latest IC | latest IR |")
    lines.append("|---|---|---|---|")
    for f in registry.list_all():
        latest = evaluator.get_latest(f.name, f.factor_version) if evaluator else None
        ic = f"{latest.ic_mean:+.3f}" if latest and latest.ic_mean is not None else "—"
        ir = f"{latest.ir:+.3f}" if latest and latest.ir is not None else "—"
        lines.append(f"| {f.name} | {f.direction} | {ic} | {ir} |")
    lines.append("")

    # 3) 历史 proposal 统计：按 (base, op) 聚合 rejection rate
    counts: dict[tuple[str, str], dict[str, int]] = {}
    recent_rejected: list[tuple[str, str]] = []
    recent_names: list[str] = []
    for p in store.list_recent(limit=200):
        try:
            recipe = json.loads(p.recipe_json)
            key = (recipe.get("base", "?"), recipe.get("op", "?"))
            counts.setdefault(key, {"total": 0, "rejected": 0, "accepted": 0})
            counts[key]["total"] += 1
            if len(recent_names) < 10:
                recent_names.append(p.factor_name)
            if p.status == "rejected":
                counts[key]["rejected"] += 1
                if len(recent_rejected) < 10 and p.reason:
                    recent_rejected.append((p.factor_name, p.reason))
            elif p.status in ("accepted", "shadow"):
                counts[key]["accepted"] += 1
        except Exception:
            continue

    if counts:
        lines.append("# 历史 proposal 拒绝率（按 base × op 聚合）")
        lines.append("")
        lines.append("| base | op | total | accepted | rejected | reject_rate |")
        lines.append("|---|---|---|---|---|---|")
        sorted_keys = sorted(counts.keys(), key=lambda k: -counts[k]["total"])
        for (base, op) in sorted_keys[:25]:
            c = counts[(base, op)]
            rate = c["rejected"] / c["total"] if c["total"] else 0
            lines.append(f"| {base} | {op} | {c['total']} | {c['accepted']} | {c['rejected']} | {rate:.0%} |")
        lines.append("")
        lines.append("最近 proposal 名称样本：" + ", ".join(recent_names))
        lines.append("")

    # 4) 最近 rejection reason 抽样
    if recent_rejected:
        lines.append("# 最近 rejection 原因（避开类似配置）")
        lines.append("")
        for name, reason in recent_rejected:
            lines.append(f"- `{name}`: {reason[:100]}")
        lines.append("")

    return "\n".join(lines)
