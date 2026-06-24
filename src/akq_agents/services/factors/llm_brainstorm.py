"""LLM-based factor brainstorming.

让大模型看现状 → 提出新 recipe → 入库为 llm_suggested。
**不**直接进 OOS 流程；需人工审核。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from akq_agents.services.factors.discovery import _BASES, _OPS, _WINDOWS, _DIRECTIONS
from akq_agents.services.factors.proposal_store import (
    FactorProposal, FactorProposalStore, now_iso, recipe_to_json,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "factor_brainstorm.md"


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


def _validate_recipe(recipe: dict) -> str | None:
    """返回错误信息字符串；None 表示合法。"""
    if not isinstance(recipe, dict):
        return "recipe must be dict"
    for key in ("base", "op", "window", "direction"):
        if key not in recipe:
            return f"missing key: {key}"
    if recipe["base"] not in _BASES:
        return f"unknown base: {recipe['base']!r} (allowed: {list(_BASES.keys())})"
    if recipe["op"] not in _OPS:
        return f"unknown op: {recipe['op']!r} (allowed: {list(_OPS)})"
    if recipe["window"] not in _WINDOWS:
        return f"unknown window: {recipe['window']!r} (allowed: {list(_WINDOWS)})"
    if recipe["direction"] not in _DIRECTIONS:
        return f"unknown direction: {recipe['direction']!r}"
    # 归一化 window：LLM 可能输出 5.0 而非 5；5.0 in (5,...) 为 True 通过校验，
    # 但 recipe_to_json 会序列化成 "5.0" 让 hash 漂移，导致同含义 recipe 拿到不同 name → dedup 失效。
    recipe["window"] = int(recipe["window"])
    return None


def _recipe_to_name(recipe: dict) -> str:
    """生成稳定的因子名：llm_{op}_{base}_{window}_{direction}_{hash6}。"""
    canonical = recipe_to_json(recipe)
    h = hashlib.md5(canonical.encode()).hexdigest()[:6]
    return f"llm_{recipe['op']}_{recipe['base']}_{recipe['window']}_{recipe['direction']}_{h}"


def _parse_llm_response(text: str) -> list[dict]:
    """从 LLM 返回里提取 suggestions 列表。

    宽容点：找最外层 {...} 即可（兼容 ```json fence、前后多余文字）。
    """
    i = text.find("{")
    j = text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
    data = json.loads(text[i:j + 1])
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        raise ValueError(f"LLM output missing 'suggestions' list: keys={list(data.keys())}")
    return suggestions


class LLMFactorBrainstormer:
    """让 LLM 提因子，写入 factor_proposals 为 llm_suggested 等人工审核。"""

    def __init__(
        self,
        *,
        llm_client: Any,
        proposal_store: FactorProposalStore,
        registry: Any,
        evaluator: Any,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout_s: int = 60,
    ) -> None:
        self._llm = llm_client
        self._store = proposal_store
        self._registry = registry
        self._evaluator = evaluator
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    def run(self, *, n: int) -> dict[str, int]:
        """执行一次 brainstorm，返回 stats。"""
        context = build_state_summary(self._registry, self._evaluator, self._store)
        user_msg = (
            f"{context}\n\n---\n\n请给出 **{n}** 个新候选 recipe。"
            f"严格 JSON 输出，no extra text。"
        )

        stats = {"requested": n, "accepted_into_review": 0, "invalid": 0, "duplicate": 0, "errors": 0}
        try:
            resp = self._llm.chat(
                model=self._model,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                timeout_s=self._timeout_s,
            )
            suggestions = _parse_llm_response(resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM brainstorm failed: %s", exc)
            stats["errors"] += 1
            return stats

        for s in suggestions[:n]:
            recipe = s.get("recipe") if isinstance(s, dict) else None
            rationale = s.get("rationale", "") if isinstance(s, dict) else ""
            err = _validate_recipe(recipe or {})
            if err is not None:
                logger.info("invalid LLM recipe: %s", err)
                stats["invalid"] += 1
                continue
            assert isinstance(recipe, dict)
            name = _recipe_to_name(recipe)
            recipe_json_str = recipe_to_json(recipe)
            # M18-I3: 跨 auto_/llm_ 命名空间查重 — 同 recipe 即使被 auto discovery
            # 拒绝过 (auto_xxx 名), 也不要让 LLM 重做一遍 (llm_xxx 名)
            existing_name = self._store.exists_recipe(recipe_json_str)
            if existing_name is not None:
                logger.info(
                    "LLM 提议的 recipe 已存在 (existing=%s, would-be=%s), 跳过",
                    existing_name, name,
                )
                stats["duplicate"] += 1
                continue
            # 兜底: 同名 (理论上不会进入这里因为 hash 包含 recipe)
            if self._store.exists(name):
                stats["duplicate"] += 1
                continue
            self._store.upsert(FactorProposal(
                factor_name=name,
                recipe_json=recipe_json_str,
                direction=recipe["direction"],
                status="llm_suggested",
                ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
                reason=f"LLM suggested: {rationale[:300]}",
                created_at=now_iso(),
                evaluated_at=None,
            ))
            stats["accepted_into_review"] += 1
        return stats
