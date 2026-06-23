# LLM 因子构建方向（Brainstorm）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 看完系统现状（DSL 能力圈 + 现有 154 个 proposal + 7 预置因子 + 3 accepted 现状）后，输出**结构化 recipe + 文字理由**，写入 `factor_proposals` 表新状态 `llm_suggested`；走人工审核流，通过后 → 复用现有 OOS 流程上线。

**Architecture:**
- 新增 `LLMFactorBrainstormer` service：组装现状 → 调 chat 模型 → 解析 recipe → 入库 `llm_suggested`
- 新增 daemon job `factor.brainstorm`（每日 20:00 cron，20 个建议）
- 复用现有 discovery 的 `_RuntimeFactor`、`FactorProposalStore`、`FactorEvaluator`，**不动**它们的内部逻辑
- `/research` 页面加"让 LLM 提建议"按钮 + LLM 提议列表 + 接受/拒绝按钮

**Tech Stack:**
- Python 3.10, FastAPI, APScheduler（已有）
- 复用 `LLMOrchestrator.run_analyst()`（无 ToolUse 单次调用足够）
- 复用 `factor_proposals` 表 + 加新 status 值 `llm_suggested`（兼容现有 schema）

---

## 文件结构

新建：
- `src/akq_agents/services/factors/llm_brainstorm.py` — Brainstormer 主服务（context 组装 + LLM 调用 + 解析 + 入库）
- `src/akq_agents/agents/prompts/factor_brainstorm.md` — system prompt
- `src/akq_agents/orchestrator/jobs/factor_brainstorm.py` — daemon job 入口
- `tests/factors/test_llm_brainstorm.py` — 单元测试

修改：
- `src/akq_agents/services/factors/proposal_store.py` — `list_recent` 加 `status='llm_suggested'` 兼容（其实已支持，只需文档+测试确认）
- `src/akq_agents/models/scheduler_config.py` — 加 `FactorBrainstormConfig`
- `src/akq_agents/orchestrator/scheduler.py:222` — 注册新 job
- `src/akq_agents/bootstrap.py` — 装配 `LLMFactorBrainstormer` 到 services
- `src/akq_agents/web/api/research.py` — 加 3 个 endpoint：`POST /api/research/factors/brainstorm/run`、`GET /api/research/factors/llm-suggestions`、`POST /api/research/factors/llm-suggestions/{name}/{action}`（action=accept|reject）
- `src/akq_agents/web/templates/research.html.j2` — UI 加按钮 + 建议列表

---

## 关键设计决定

1. **`llm_suggested` 是 `factor_proposals.status` 的新枚举值**，schema 不变（status 列本来就是 TEXT 不带约束）
2. **接受流程**：人工"接受" → status 从 `llm_suggested` → `shadow`，**复用现有 `DiscoveryEngine` 的 shadow→accepted OOS 流程**——下一轮 discovery 自然会跑这个新 shadow 因子
3. **DSL 边界**：LLM 只能从既有 base/op/window/direction 组合，输出非法值就 reject（不让 LLM 设计新 op）
4. **去重**：LLM 提议如果撞上已有 `auto_*` 或之前的 `llm_*`，跳过不重复入库
5. **失败兜底**：LLM 调用失败 / JSON 解析失败 → 写 `events` 表 ERROR 级，不抛
6. **触发**：daemon 每日 20:00 cron + `/research` 页面按钮

---

## Task 1：Schema 与数据契约确认

**Files:**
- Test: `tests/factors/test_llm_brainstorm.py`（新建）

- [ ] **Step 1: 写测试确认 `factor_proposals` 表能装 `status='llm_suggested'`**

```python
"""tests/factors/test_llm_brainstorm.py"""
from pathlib import Path

from akq_agents.services.factors.proposal_store import (
    FactorProposal, FactorProposalStore, now_iso,
)


def test_proposal_store_accepts_llm_suggested_status(tmp_path: Path) -> None:
    store = FactorProposalStore(tmp_path / "meta.db")
    p = FactorProposal(
        factor_name="llm_test_close_pct_change_5_long",
        recipe_json='{"base":"close","op":"pct_change","window":5,"direction":"long"}',
        direction="long",
        status="llm_suggested",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="LLM suggested: 短期动量在 high-volatility 区间有效",
        created_at=now_iso(),
        evaluated_at=None,
    )
    store.upsert(p)
    rows = store.list_recent(status="llm_suggested")
    assert len(rows) == 1
    assert rows[0].factor_name == "llm_test_close_pct_change_5_long"
    assert rows[0].status == "llm_suggested"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `pytest tests/factors/test_llm_brainstorm.py::test_proposal_store_accepts_llm_suggested_status -v`
Expected: PASS（schema 已经支持任意 status 字符串，应该一次过）

- [ ] **Step 3: Commit**

```bash
git add tests/factors/test_llm_brainstorm.py
git commit -m "test(factors): 确认 factor_proposals 支持 llm_suggested status"
```

---

## Task 2：现状摘要器（StateSummarizer）

**Files:**
- Create: `src/akq_agents/services/factors/llm_brainstorm.py`
- Test: `tests/factors/test_llm_brainstorm.py`（修改）

目标：把 7 预置因子 + 154 proposals + 3 accepted 整理成一份 ~2K tokens 的紧凑 markdown 摘要给 LLM 看。

- [ ] **Step 1: 写测试**

在 `tests/factors/test_llm_brainstorm.py` 追加：

```python
from unittest.mock import MagicMock

from akq_agents.services.factors.llm_brainstorm import build_state_summary


def test_build_state_summary_includes_dsl_and_stats(tmp_path: Path) -> None:
    # mock registry
    registry = MagicMock()
    f1 = MagicMock(name="f1", direction="long", lookback_days=60)
    f1.name = "momentum_20"
    f2 = MagicMock(name="f2", direction="short", lookback_days=60)
    f2.name = "volatility_20"
    registry.list_all.return_value = [f1, f2]

    # mock evaluator: 给 momentum_20 一个 latest IC/IR
    evaluator = MagicMock()
    latest = MagicMock(ic_mean=0.04, ir=0.45, window_days=60, as_of_date="2026-06-22")
    evaluator.get_latest.side_effect = lambda name, ver: latest if name == "momentum_20" else None

    # mock proposal store
    store = FactorProposalStore(tmp_path / "meta.db")
    store.upsert(FactorProposal(
        factor_name="auto_zscore_close_20_long_a1b2",
        recipe_json='{"base":"close","op":"zscore","window":20,"direction":"long"}',
        direction="long", status="accepted",
        ic_mean=0.03, ic_std=0.5, ir=0.4, t_stat=2.1, max_abs_corr=0.5,
        reason="ok", created_at=now_iso(), evaluated_at=now_iso(),
    ))

    md = build_state_summary(registry, evaluator, store)

    # 必须包含的内容
    assert "close" in md and "volume" in md  # DSL base
    assert "pct_change" in md and "zscore" in md  # DSL op
    assert "momentum_20" in md
    assert "auto_zscore_close_20_long_a1b2" in md
    # 长度合理（~2K tokens 上限）
    assert 500 < len(md) < 8000
```

- [ ] **Step 2: 写最小实现**

新建 `src/akq_agents/services/factors/llm_brainstorm.py`：

```python
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
    for p in store.list_recent(limit=200):
        try:
            recipe = json.loads(p.recipe_json)
            key = (recipe.get("base", "?"), recipe.get("op", "?"))
            counts.setdefault(key, {"total": 0, "rejected": 0, "accepted": 0})
            counts[key]["total"] += 1
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

    # 4) 最近 rejection reason 抽样
    if recent_rejected:
        lines.append("# 最近 rejection 原因（避开类似配置）")
        lines.append("")
        for name, reason in recent_rejected:
            lines.append(f"- `{name}`: {reason[:100]}")
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 3: 跑测试**

Run: `pytest tests/factors/test_llm_brainstorm.py -v`
Expected: PASS（2 个测试都过）

- [ ] **Step 4: Commit**

```bash
git add src/akq_agents/services/factors/llm_brainstorm.py tests/factors/test_llm_brainstorm.py
git commit -m "feat(factors): 因子现状摘要器（给 LLM 看的 markdown context）"
```

---

## Task 3：System Prompt

**Files:**
- Create: `src/akq_agents/agents/prompts/factor_brainstorm.md`

- [ ] **Step 1: 写 prompt**

```markdown
# 角色

你是 A 股量化因子研究员。你的任务是**根据现有因子表现，提出新的候选因子 recipe**。

# 输入

我会给你一份 markdown 现状报告，包含：
1. 你能用的 DSL（base / op / window / direction，**不能超出此范围**）
2. 当前已上线（accepted）因子及其 IC / IR
3. 历史 proposal 按 (base, op) 聚合的拒绝率
4. 最近被拒绝的因子和拒绝原因（参考避开）

# 输出格式（严格 JSON）

```json
{
  "suggestions": [
    {
      "recipe": {"base": "close", "op": "zscore", "window": 30, "direction": "long"},
      "rationale": "中期动量 zscore 标准化能在波动率高的市场更稳定。当前 accepted 因子里只有 momentum_60，缺少 zscore 化的中期信号。"
    }
  ]
}
```

**硬约束**：
- `base` 必须在：close, volume, amount, high_low_range, vwap
- `op` 必须在：pct_change, rolling_mean, rolling_std, zscore, rsi, rolling_skew, ts_max_norm, ts_min_norm
- `window` 必须在：5, 10, 20, 30, 60
- `direction` 必须在：long, short
- 不允许虚构新参数
- 输出**只有** JSON，不要任何额外说明

# 策略提示

- 优先填补**未被探索的组合**（看历史拒绝率表里 total 为 0 的格子）
- 但要警惕高拒绝率的 (base, op) 区域——别强行往那里推
- direction 选择需要逻辑：`pct_change/momentum` 类通常 `long`，`volatility/std` 类通常 `short`
- 中国 A 股短期（5 天）反转效应明显，中长期（20+）动量有效——你的提议要符合这种经验

# 目标数量

我会告诉你需要 N 个 suggestion，请精确产出 N 个，不多不少。
```

- [ ] **Step 2: Commit**

```bash
git add src/akq_agents/agents/prompts/factor_brainstorm.md
git commit -m "feat(factors): LLM brainstorm system prompt"
```

---

## Task 4：LLMFactorBrainstormer（核心服务）

**Files:**
- Modify: `src/akq_agents/services/factors/llm_brainstorm.py`
- Test: `tests/factors/test_llm_brainstorm.py`

- [ ] **Step 1: 写测试 — recipe 合法性校验**

在 `tests/factors/test_llm_brainstorm.py` 追加：

```python
from akq_agents.services.factors.llm_brainstorm import (
    LLMFactorBrainstormer, _validate_recipe, _recipe_to_name,
)


def test_validate_recipe_accepts_valid() -> None:
    assert _validate_recipe({
        "base": "close", "op": "pct_change", "window": 5, "direction": "long",
    }) is None


def test_validate_recipe_rejects_unknown_op() -> None:
    err = _validate_recipe({
        "base": "close", "op": "ema",  # 不在 _OPS
        "window": 5, "direction": "long",
    })
    assert err is not None and "op" in err


def test_validate_recipe_rejects_unknown_window() -> None:
    err = _validate_recipe({
        "base": "close", "op": "pct_change", "window": 7,  # 不在 _WINDOWS
        "direction": "long",
    })
    assert err is not None and "window" in err


def test_recipe_to_name_is_deterministic() -> None:
    r = {"base": "close", "op": "zscore", "window": 30, "direction": "long"}
    assert _recipe_to_name(r) == _recipe_to_name(r)
    assert _recipe_to_name(r).startswith("llm_")
```

- [ ] **Step 2: 写测试 — Brainstormer 主流程（mock LLM）**

继续追加：

```python
def test_brainstormer_writes_valid_suggestions_to_store(tmp_path: Path) -> None:
    # mock LLM 返回 2 个 suggestion，1 合法 1 非法（unknown op）
    llm_client = MagicMock()
    llm_resp = MagicMock()
    llm_resp.text = '''
    {"suggestions": [
        {"recipe": {"base":"close","op":"zscore","window":30,"direction":"long"},
         "rationale": "中期 zscore 动量"},
        {"recipe": {"base":"close","op":"ema","window":10,"direction":"long"},
         "rationale": "新算子（不合法）"}
    ]}
    '''
    llm_resp.prompt_tokens = 100
    llm_resp.completion_tokens = 50
    llm_client.chat.return_value = llm_resp

    store = FactorProposalStore(tmp_path / "meta.db")

    # mock registry/evaluator (空)
    registry = MagicMock(list_all=MagicMock(return_value=[]))
    evaluator = MagicMock(get_latest=MagicMock(return_value=None))

    brainstormer = LLMFactorBrainstormer(
        llm_client=llm_client,
        proposal_store=store,
        registry=registry,
        evaluator=evaluator,
        model="test-model",
        max_tokens=2000,
        temperature=1.0,
    )

    stats = brainstormer.run(n=2)

    # 1 个合法入库，1 个非法被拒
    assert stats["requested"] == 2
    assert stats["accepted_into_review"] == 1
    assert stats["invalid"] == 1

    rows = store.list_recent(status="llm_suggested")
    assert len(rows) == 1
    assert "zscore" in rows[0].recipe_json
    assert "中期 zscore 动量" in (rows[0].reason or "")


def test_brainstormer_skips_duplicate_recipe(tmp_path: Path) -> None:
    """同一 recipe 第二次提议应被识别为重复跳过。"""
    store = FactorProposalStore(tmp_path / "meta.db")
    # 先放一条已存在的
    store.upsert(FactorProposal(
        factor_name=_recipe_to_name({"base":"close","op":"zscore","window":30,"direction":"long"}),
        recipe_json='{"base":"close","op":"zscore","window":30,"direction":"long"}',
        direction="long", status="rejected",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="too low IR", created_at=now_iso(), evaluated_at=now_iso(),
    ))

    llm_client = MagicMock()
    llm_resp = MagicMock(
        text='{"suggestions":[{"recipe":{"base":"close","op":"zscore","window":30,"direction":"long"},"rationale":"x"}]}',
        prompt_tokens=10, completion_tokens=10,
    )
    llm_client.chat.return_value = llm_resp

    registry = MagicMock(list_all=MagicMock(return_value=[]))
    evaluator = MagicMock(get_latest=MagicMock(return_value=None))

    brainstormer = LLMFactorBrainstormer(
        llm_client=llm_client, proposal_store=store,
        registry=registry, evaluator=evaluator,
        model="test-model", max_tokens=2000, temperature=1.0,
    )
    stats = brainstormer.run(n=1)
    assert stats["duplicate"] == 1
    assert stats["accepted_into_review"] == 0
```

- [ ] **Step 3: 写实现**

在 `src/akq_agents/services/factors/llm_brainstorm.py` 追加：

```python
import hashlib
import re
from pathlib import Path

from akq_agents.services.factors.proposal_store import (
    FactorProposal, FactorProposalStore, now_iso, recipe_to_json,
)


_PROMPT_PATH = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "factor_brainstorm.md"


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
    return None


def _recipe_to_name(recipe: dict) -> str:
    """生成稳定的因子名：llm_{op}_{base}_{window}_{direction}_{hash6}。

    hash 是为了规避未来同 recipe 但语义略有差异时的冲突；当前其实没必要，
    但保持和 auto_* 一致的命名风格。
    """
    canonical = recipe_to_json(recipe)
    h = hashlib.md5(canonical.encode()).hexdigest()[:6]
    return f"llm_{recipe['op']}_{recipe['base']}_{recipe['window']}_{recipe['direction']}_{h}"


def _parse_llm_response(text: str) -> list[dict]:
    """从 LLM 返回里提取 suggestions 列表。

    宽容点：允许 ```json ... ``` fence、前后有多余文字。
    """
    # 优先找 ```json fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    # 找最外层 {...}
    if not raw.lstrip().startswith("{"):
        i = raw.find("{")
        j = raw.rfind("}")
        if i < 0 or j < 0:
            raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
        raw = raw[i:j+1]
    data = json.loads(raw)
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        raise ValueError(f"LLM output missing 'suggestions' list: keys={list(data.keys())}")
    return suggestions


class LLMFactorBrainstormer:
    """让 LLM 提因子，写入 factor_proposals 为 llm_suggested 等人工审核。

    依赖：
    - llm_client: LLMClient (akq_agents.services.llm.client)
    - proposal_store: FactorProposalStore
    - registry: FactorRegistry（用来摘要现状）
    - evaluator: FactorEvaluator（用来摘要 IC/IR；可为 None）
    """

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
            name = _recipe_to_name(recipe)
            if self._store.exists(name):
                stats["duplicate"] += 1
                continue
            self._store.upsert(FactorProposal(
                factor_name=name,
                recipe_json=recipe_to_json(recipe),
                direction=recipe["direction"],
                status="llm_suggested",
                ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
                reason=f"LLM suggested: {rationale[:300]}",
                created_at=now_iso(),
                evaluated_at=None,
            ))
            stats["accepted_into_review"] += 1
        return stats
```

- [ ] **Step 4: 跑全部 brainstorm 测试**

Run: `pytest tests/factors/test_llm_brainstorm.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/akq_agents/services/factors/llm_brainstorm.py tests/factors/test_llm_brainstorm.py
git commit -m "feat(factors): LLMFactorBrainstormer — LLM 提 recipe 入库 llm_suggested"
```

---

## Task 5：daemon job（每日 20:00）

**Files:**
- Create: `src/akq_agents/orchestrator/jobs/factor_brainstorm.py`
- Modify: `src/akq_agents/models/scheduler_config.py`（加 `FactorBrainstormConfig`）
- Modify: `src/akq_agents/orchestrator/scheduler.py:222`（register 调用）
- Modify: `src/akq_agents/bootstrap.py`（装配 brainstormer 到 services）
- Test: `tests/orchestrator/test_factor_brainstorm_job.py`

- [ ] **Step 1: 加 config**

修改 `src/akq_agents/models/scheduler_config.py`：在 `FactorDiscoveryConfig` 后面加：

```python
class FactorBrainstormConfig(BaseModel):
    """LLM 因子构建方向建议 job（每日 cron 20:00）。

    走 trading_day 白名单。每次产出 n_suggestions 条 status='llm_suggested' 记录，
    等待人工 /research 页审核。
    """

    enabled: bool = True
    hour: int = 20
    minute: int = 0
    timeout_s: int = 120
    n_suggestions: int = 20
```

然后在 `SchedulerJobsConfig` 里加：

```python
    factor_brainstorm: FactorBrainstormConfig = Field(default_factory=FactorBrainstormConfig)
```

- [ ] **Step 2: 创建 job 入口**

新建 `src/akq_agents/orchestrator/jobs/factor_brainstorm.py`：

```python
"""``factor.brainstorm``：每日 20:00 cron，让 LLM 提因子构建方向。

产出写入 ``factor_proposals`` 表 status='llm_suggested'，需人工在 /research 页面
审核。审核接受后 status → 'shadow'，下一轮 factor.discovery 会接管做 OOS 评估。

仅在交易日跑（JobRunner trading-day 白名单）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "factor.brainstorm"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    job_cfg = cfg.jobs.factor_brainstorm
    if not job_cfg.enabled:
        return
    if "llm_factor_brainstormer" not in services:
        logger.info("factor.brainstorm enabled but llm_factor_brainstormer missing; skip")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(
            JOB_ID,
            partition,
            lambda: _do(services, n=job_cfg.n_suggestions),
            timeout_s=job_cfg.timeout_s,
        )

    scheduler.add_job(
        _run,
        CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    logger.info("factor.brainstorm registered at %02d:%02d, n=%d",
                job_cfg.hour, job_cfg.minute, job_cfg.n_suggestions)


def run_once_now(runner: JobRunner, services: dict[str, Any], n: int = 20) -> dict:
    """供 web /api/research/factors/brainstorm/run 手动触发。"""
    partition = date.today().isoformat()
    return runner.run(
        JOB_ID, partition,
        lambda: _do(services, n=n),
        timeout_s=120,
    )


def _do(services: dict[str, Any], *, n: int) -> dict[str, Any]:
    brainstormer = services["llm_factor_brainstormer"]
    stats = brainstormer.run(n=n)
    return stats
```

- [ ] **Step 3: 在 scheduler.py 注册**

修改 `src/akq_agents/orchestrator/scheduler.py:216-223`，在 `factor_discovery.register(...)` 后加：

```python
        from akq_agents.orchestrator.jobs import factor_brainstorm
        factor_brainstorm.register(self._scheduler, self._runner, self._cfg, self._services)
```

- [ ] **Step 4: 在 bootstrap.py 装配 brainstormer 到 services**

在 `src/akq_agents/bootstrap.py` 找到装配 `discovery_engine` 的位置（grep `discovery_engine` 或 `services["discovery_engine"]`），紧跟其后加：

```python
        # M14: LLM 因子构建方向 brainstormer（可选；llm_client 不存在则跳过）
        if llm_client is not None:
            from akq_agents.services.factors.llm_brainstorm import LLMFactorBrainstormer
            chat_cfg = llm_config.profiles.get("chat")  # 复用 chat 型号
            if chat_cfg is not None:
                services["llm_factor_brainstormer"] = LLMFactorBrainstormer(
                    llm_client=llm_client,
                    proposal_store=services["factor_proposal_store"],
                    registry=services["factor_registry"],
                    evaluator=services.get("factor_evaluator"),
                    model=chat_cfg.model,
                    max_tokens=chat_cfg.max_tokens,
                    temperature=chat_cfg.temperature,
                )
```

**注意**：先 grep `llm_config\|chat_cfg\|llm_client` 在 bootstrap.py 里确认上下文，本步骤中的字段名（`llm_config.profiles["chat"]`）可能要按实际类型调整。

- [ ] **Step 5: 写 job 测试**

新建 `tests/orchestrator/test_factor_brainstorm_job.py`：

```python
from unittest.mock import MagicMock

from akq_agents.orchestrator.jobs.factor_brainstorm import _do


def test_do_delegates_to_brainstormer() -> None:
    brainstormer = MagicMock()
    brainstormer.run.return_value = {"requested": 5, "accepted_into_review": 3,
                                       "invalid": 1, "duplicate": 1, "errors": 0}
    services = {"llm_factor_brainstormer": brainstormer}
    out = _do(services, n=5)
    brainstormer.run.assert_called_once_with(n=5)
    assert out["accepted_into_review"] == 3
```

- [ ] **Step 6: 跑测试 + 静态检查**

```bash
pytest tests/orchestrator/test_factor_brainstorm_job.py tests/factors/test_llm_brainstorm.py -v
python -c "import ast; ast.parse(open('src/akq_agents/orchestrator/jobs/factor_brainstorm.py').read()); ast.parse(open('src/akq_agents/orchestrator/scheduler.py').read()); ast.parse(open('src/akq_agents/bootstrap.py').read()); print('ok')"
```

Expected: 全部 PASS + py syntax ok

- [ ] **Step 7: Commit**

```bash
git add src/akq_agents/orchestrator/jobs/factor_brainstorm.py \
        src/akq_agents/orchestrator/scheduler.py \
        src/akq_agents/bootstrap.py \
        src/akq_agents/models/scheduler_config.py \
        tests/orchestrator/test_factor_brainstorm_job.py
git commit -m "feat(factors): factor.brainstorm daemon job + 装配到 services"
```

---

## Task 6：Web API 接入

**Files:**
- Modify: `src/akq_agents/web/api/research.py`
- Test: `tests/web/test_research_brainstorm.py`

加 3 个端点：

1. `POST /api/research/factors/brainstorm/run` — on-demand 触发
2. `GET /api/research/factors/llm-suggestions` — 列出待审核
3. `POST /api/research/factors/llm-suggestions/{factor_name}/{action}` — accept / reject

- [ ] **Step 1: 写测试**

新建 `tests/web/test_research_brainstorm.py`：

```python
"""LLM brainstorm web 端点测试。
依赖 tests/web/conftest.py 提供的 client + container fixtures。
"""
from unittest.mock import MagicMock


def test_list_llm_suggestions_empty(client) -> None:
    r = client.get("/api/research/factors/llm-suggestions")
    assert r.status_code == 200
    assert r.json() == {"suggestions": [], "n": 0}


def test_brainstorm_run_calls_job(client, container) -> None:
    """POST /api/research/factors/brainstorm/run 应调用 run_once_now 并返回 stats。"""
    # 把 fake brainstormer 注入 container.workflow.services
    fake_brainstormer = MagicMock()
    fake_brainstormer.run.return_value = {
        "requested": 5, "accepted_into_review": 3, "invalid": 1, "duplicate": 1, "errors": 0,
    }
    container.workflow.services["llm_factor_brainstormer"] = fake_brainstormer
    # job_runner 也得有
    container.workflow.services["job_runner"] = MagicMock(
        run=lambda job_id, partition, fn, timeout_s=120: {"stats": fn()},
    )

    r = client.post("/api/research/factors/brainstorm/run", json={"n": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["accepted_into_review"] == 3


def test_accept_llm_suggestion_promotes_to_shadow(client, container) -> None:
    """POST /api/research/factors/llm-suggestions/{name}/accept 应改 status 为 shadow。"""
    # 先在 store 里塞一条 llm_suggested
    from akq_agents.services.factors.proposal_store import FactorProposal, now_iso, recipe_to_json
    container.workflow.services["factor_proposal_store"].upsert(FactorProposal(
        factor_name="llm_test_001",
        recipe_json=recipe_to_json({"base":"close","op":"zscore","window":30,"direction":"long"}),
        direction="long", status="llm_suggested",
        ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
        reason="LLM: test rationale", created_at=now_iso(), evaluated_at=None,
    ))

    r = client.post("/api/research/factors/llm-suggestions/llm_test_001/accept")
    assert r.status_code == 200
    assert r.json()["status"] == "shadow"

    # 验证 DB 状态
    rows = container.workflow.services["factor_proposal_store"].list_recent(status="shadow")
    assert any(r.factor_name == "llm_test_001" for r in rows)
```

**注意**：conftest 可能还没暴露 `container` fixture。先 `cat tests/web/conftest.py` 确认现有 fixture 名字，按需调整。

- [ ] **Step 2: 实现端点**

修改 `src/akq_agents/web/api/research.py` 末尾追加：

```python
# ============================================================
# M14: LLM 因子构建方向（brainstorm）
# ============================================================

@router.get("/factors/llm-suggestions")
async def llm_suggestions_list(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """列出 status='llm_suggested' 的待审核提议。"""
    svc: ServiceContainer = get_services()
    if svc.workflow is None:
        return {"suggestions": [], "n": 0}
    store = svc.workflow.services.get("factor_proposal_store")
    if store is None:
        return {"suggestions": [], "n": 0}
    rows = store.list_recent(limit=limit, status="llm_suggested")
    return {
        "suggestions": [
            {
                "factor_name": r.factor_name,
                "recipe": json.loads(r.recipe_json),
                "direction": r.direction,
                "reason": r.reason,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "n": len(rows),
    }


@router.post("/factors/brainstorm/run")
async def trigger_brainstorm(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """手动触发一次 LLM brainstorm（同步等结果）。"""
    svc: ServiceContainer = get_services()
    if svc.workflow is None:
        raise HTTPException(503, detail="workflow not ready")
    services = svc.workflow.services
    if "llm_factor_brainstormer" not in services:
        raise HTTPException(503, detail="llm_factor_brainstormer not configured (检查 LLM 是否启用)")
    if "job_runner" not in services:
        raise HTTPException(503, detail="job_runner not available")
    n = int((payload or {}).get("n", 10))
    n = max(1, min(n, 30))

    from akq_agents.orchestrator.jobs.factor_brainstorm import run_once_now
    out = run_once_now(services["job_runner"], services, n=n)
    return {"ok": True, "stats": out}


@router.post("/factors/llm-suggestions/{factor_name}/{action}")
async def review_llm_suggestion(factor_name: str, action: str) -> dict[str, Any]:
    """人工审核 LLM 提议：action=accept → status='shadow'，reject → status='rejected'。"""
    if action not in ("accept", "reject"):
        raise HTTPException(400, detail=f"action must be accept|reject, got {action!r}")
    svc: ServiceContainer = get_services()
    if svc.workflow is None:
        raise HTTPException(503, detail="workflow not ready")
    store = svc.workflow.services.get("factor_proposal_store")
    if store is None:
        raise HTTPException(503, detail="proposal_store not available")

    # 找现有记录
    from akq_agents.services.data.repository import open_meta_db
    db_path = svc.repo._base_dir / "meta.db"
    with open_meta_db(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM factor_proposals WHERE factor_name = ?",
            (factor_name,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"factor not found: {factor_name}")
        if row[0] != "llm_suggested":
            raise HTTPException(409, detail=f"factor status is {row[0]!r}, not 'llm_suggested'")

        new_status = "shadow" if action == "accept" else "rejected"
        # accept 时同时记 shadow_started_at（让现有 OOS 流程能算时长）
        if action == "accept":
            conn.execute(
                "UPDATE factor_proposals SET status=?, shadow_started_at=?, evaluated_at=? "
                "WHERE factor_name=?",
                (new_status, _now_iso(), _now_iso(), factor_name),
            )
        else:
            conn.execute(
                "UPDATE factor_proposals SET status=?, evaluated_at=? WHERE factor_name=?",
                (new_status, _now_iso(), factor_name),
            )
        conn.commit()
    return {"ok": True, "factor_name": factor_name, "status": new_status}


def _now_iso() -> str:
    from akq_agents.services.factors.proposal_store import now_iso
    return now_iso()
```

- [ ] **Step 3: 跑测试**

```bash
pytest tests/web/test_research_brainstorm.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/akq_agents/web/api/research.py tests/web/test_research_brainstorm.py
git commit -m "feat(web): /api/research/factors/brainstorm + llm-suggestions endpoints"
```

---

## Task 7：前端 UI（research 页加按钮 + 列表）

**Files:**
- Modify: `src/akq_agents/web/templates/research.html.j2`

UI 目标（最小）：
- 顶部一个"让 LLM 提因子方向"按钮，点击后 POST `/api/research/factors/brainstorm/run`，等候返回，刷新下方列表
- 下方一个表格列出 `llm_suggested` 状态的提议：name | recipe | direction | reason | [接受] [拒绝]

- [ ] **Step 1: 找到 research.html.j2 现有结构**

```bash
grep -n 'h1\|card\|factors\|因子' src/akq_agents/web/templates/research.html.j2 | head -20
```

按结果决定插入位置（一般是在"因子列表"卡片下方加一个新 card）。

- [ ] **Step 2: 加 UI 段落**

在 `research.html.j2` 现有因子列表卡片**下方**追加一个新 card（伪代码，注意按文件实际格式调整）：

```html
<div class="card">
  <h2>LLM 因子构建建议
    <span class="small">（status=llm_suggested，需人工审核）</span>
  </h2>
  <div class="toolbar">
    <button onclick="runBrainstorm()" class="primary" id="btn-brainstorm">让 LLM 提 10 个方向</button>
    <span id="brainstorm-status" class="small"></span>
  </div>
  <div id="llm-suggestions-table" style="overflow:auto; max-height:520px"></div>
</div>

<script>
async function runBrainstorm() {
  const btn = document.getElementById('btn-brainstorm');
  const status = document.getElementById('brainstorm-status');
  btn.disabled = true; btn.classList.add('is-loading');
  status.textContent = '调用 LLM 中…（一般 5-15 秒）';
  try {
    const r = await fetch('/api/research/factors/brainstorm/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({n: 10}),
    });
    const d = await r.json();
    if (!r.ok) { status.textContent = '❌ ' + (d.detail || JSON.stringify(d)); return; }
    const s = d.stats || {};
    status.textContent = `✅ 入库 ${s.accepted_into_review} 条，重复 ${s.duplicate}，非法 ${s.invalid}，错误 ${s.errors}`;
    loadLLMSuggestions();
  } catch (e) {
    status.textContent = '❌ 网络错误：' + e.message;
  } finally {
    btn.disabled = false; btn.classList.remove('is-loading');
  }
}

async function loadLLMSuggestions() {
  try {
    const r = await fetch('/api/research/factors/llm-suggestions?limit=50');
    const d = await r.json();
    document.getElementById('llm-suggestions-table').innerHTML = renderLLMSuggestions(d.suggestions || []);
  } catch (e) {
    document.getElementById('llm-suggestions-table').innerHTML = '<div class="empty-state">加载失败：' + e.message + '</div>';
  }
}

function renderLLMSuggestions(rows) {
  if (!rows.length) return '<div class="empty-state"><div class="empty-state-icon">∅</div><div>暂无待审核的 LLM 提议</div></div>';
  let html = '<table><thead><tr><th>因子名</th><th>recipe</th><th>方向</th><th>LLM 理由</th><th>创建</th><th>操作</th></tr></thead><tbody>';
  for (const r of rows) {
    const recipe = `${r.recipe.base}/${r.recipe.op}/w${r.recipe.window}`;
    html += `<tr>
      <td><code>${r.factor_name}</code></td>
      <td>${recipe}</td>
      <td>${r.direction}</td>
      <td class="small">${(r.reason || '').replace(/^LLM suggested:\s*/,'')}</td>
      <td class="small">${(r.created_at || '').slice(0,16)}</td>
      <td>
        <button onclick="reviewLLM('${r.factor_name}','accept')">接受</button>
        <button onclick="reviewLLM('${r.factor_name}','reject')">拒绝</button>
      </td>
    </tr>`;
  }
  return html + '</tbody></table>';
}

async function reviewLLM(name, action) {
  try {
    const r = await fetch(`/api/research/factors/llm-suggestions/${name}/${action}`, {method:'POST'});
    const d = await r.json();
    if (!r.ok) { alert('失败：' + (d.detail || JSON.stringify(d))); return; }
    loadLLMSuggestions();
  } catch (e) { alert('网络错误：' + e.message); }
}

window.addEventListener('load', loadLLMSuggestions);
</script>
```

- [ ] **Step 3: 重启 web 验证 UI**

```bash
./start.sh stop && sleep 2 && ./start.sh up
# 浏览器打开 http://127.0.0.1:8765/research
# 点击"让 LLM 提 10 个方向"，等返回，确认表格出现条目
# 任意一条点"接受"，刷新后该条消失，回到 /research 的"因子列表"卡片应能看到 status=shadow
```

- [ ] **Step 4: Commit**

```bash
git add src/akq_agents/web/templates/research.html.j2
git commit -m "feat(web): /research 页加 LLM 因子建议 UI（按钮 + 审核列表）"
```

---

## Task 8：端到端验证 + 文档

- [ ] **Step 1: 跑全量测试**

```bash
pytest tests/factors/test_llm_brainstorm.py tests/orchestrator/test_factor_brainstorm_job.py tests/web/test_research_brainstorm.py -v
```

Expected: 全部 PASS

- [ ] **Step 2: 真实 daemon 验证（如果时间允许）**

```bash
# 手动触发一次（不等 20:00 cron）
curl -X POST http://127.0.0.1:8765/api/research/factors/brainstorm/run -H 'Content-Type: application/json' -d '{"n": 5}'
```

Expected: 返回 `{"ok": true, "stats": {"requested": 5, "accepted_into_review": N, ...}}`，且：
```bash
curl http://127.0.0.1:8765/api/research/factors/llm-suggestions | python -m json.tool
```
应能看到 N 条记录。

- [ ] **Step 3: events / job_runs 检查**

```bash
sqlite3 data/meta.db "SELECT ts, kind, payload_json FROM events WHERE kind LIKE 'factor.brainstorm%' ORDER BY ts DESC LIMIT 5;"
sqlite3 data/meta.db "SELECT job_id, partition, status, finished_at FROM job_runs WHERE job_id='factor.brainstorm' ORDER BY started_at DESC LIMIT 5;"
```

Expected: 看到 `factor.brainstorm.completed` 事件 + `job_runs` 表有对应记录

- [ ] **Step 4: Commit**

```bash
git status  # 应该 clean
# 不需要额外 commit
```

---

## Self-Review

**Spec coverage:**
- ✅ LLM 产出结构化 recipe（Task 4 `_validate_recipe`）
- ✅ 入库为 `llm_suggested` status（Task 1 测试 + Task 4 实现）
- ✅ 人工审核（Task 6 endpoint + Task 7 UI）
- ✅ 接受后接入既有 OOS 流程（accept → status='shadow'，下一轮 discovery 接管）
- ✅ daemon 每日 20:00 cron 触发（Task 5 `CronTrigger(hour=20)`）
- ✅ research 页按钮（Task 7）
- ✅ 复用 chat 型号（Task 5 Step 4 装配代码读 `llm_config.profiles["chat"]`）
- ✅ 一次出 20 个建议（`FactorBrainstormConfig.n_suggestions=20`，UI 按钮上是 10 个，user-facing 默认值可调）

**Placeholder scan:**
- 仅在 Task 5 Step 4 标注"按实际类型调整" —— 这是因为 bootstrap.py 的具体字段需要看现场，无法预先 100% 写死；其它步骤都有完整代码。

**Type consistency:**
- `LLMFactorBrainstormer.__init__` 在 Task 4 定义，Task 5 装配代码字段名一致
- `factor_name` / `recipe_json` / `status` 字段在 Task 1/4/6 测试 + 实现里一致
- API path 在 Task 6 endpoint + Task 7 UI fetch URL 一致（`/api/research/factors/...`）

---

## 范围里没做的（YAGNI 留给以后）

- ❌ LLM brainstorm 历史记录页（user 直接查 DB 就够）
- ❌ rate limiting（一天 20 个 + on-demand 按钮，预算可控）
- ❌ A/B 对比 LLM vs auto discovery 谁产出更好的因子（数据积累足够后再做）
- ❌ LLM 自己迭代：根据回测结果调 recipe（需要 multi-turn，复杂度高）
- ❌ 让 LLM 提出"新 op"（破坏 DSL 边界，违反 surgical changes）
