"""LLM 自由 Python 代码因子 brainstorm (重构新增).

与 llm_dsl_brainstorm.py 的区别:
- 不限定 base × op × window × direction 笛卡尔积. LLM 写任何受 sandbox 限制的
  Python `def compute(ohlcv) -> pd.Series` 都接受.
- 跨 session 同 source_code (sha1 一致) 自动去重, 不会反复入库相同思路.
- 编译失败 / 危险代码 / 跑了超时 → 静默跳过, 不污染数据库.

输出写入 ``factor_proposals`` 表 recipe_kind='code' 字段:
- recipe_code = 原始 Python 源码
- code_hash = sha1(source_code)
- direction = LLM 自填 (long / short)
- status = 'llm_suggested' (待人工审核) 或 'llm_rejected' (编译失败时)

人工审核通过后 status → 'shadow', 下一轮 factor.discovery 会跑 shadow OOS 评估.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from akq_agents.services.factors.proposal_store import (
    FactorProposal,
    FactorProposalStore,
    now_iso,
)
from akq_agents.services.factors.sandbox import (
    CodeTimeoutError,
    UnsafeCodeError,
    compile_code_factor,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "factor_code_brainstorm.md"


# -------------------- 现状摘要 (给 LLM 看的状态报告) --------------------


def build_state_summary(
    registry: Any,
    evaluator: Any,
    store: FactorProposalStore,
) -> str:
    """组装给 LLM 看的 markdown 现状摘要.

    包含:
    - sandbox 受限 API 列表 (LLM 知道能用什么)
    - 当前已上线因子 (accepted) 名字 + IC/IR
    - 历史 code 因子拒绝率 (按 direction 聚合)
    - 最近 10 个 code 因子的 code_hash 前 8 位 + 描述 (避免 LLM 重做)
    """
    lines: list[str] = []

    # 1) sandbox 能力圈
    lines.append("# 沙箱允许的 API")
    lines.append("")
    lines.append("你可以使用以下模块/对象 — 任何 import / 反射 / 危险 builtin 都会被静态拒绝:")
    lines.append("")
    lines.append("## 数据访问")
    lines.append("- `ohlcv`: pd.DataFrame, 列: date, symbol, open, high, low, close, volume, amount")
    lines.append("- `pd` / `np` / `math`: 完整可用 (但 pd.read_* / np.load 不可用 — 没有文件)")
    lines.append("")
    lines.append("## 受限 builtin (白名单)")
    lines.append("- 数学: abs/min/max/sum/round/pow/divmod")
    lines.append("- 序列: len/range/enumerate/zip/map/filter/sorted/any/all")
    lines.append("- 容器: list/tuple/dict/set/frozenset/int/float/str/bool")
    lines.append("- 异常: Exception/ValueError/TypeError/KeyError/IndexError/RuntimeError")
    lines.append("")
    lines.append("## 禁止 (LLM 写了就拒)")
    lines.append("- 任何 import (import X / from X import Y)")
    lines.append("- 任何 open / file / 网络 / 子进程")
    lines.append("- eval / exec / getattr / globals / locals")
    lines.append("- 任何 dunder 属性访问 (`__class__`, `__subclasses__` 等)")
    lines.append("")
    lines.append("## 输出约定")
    lines.append("- 必须定义 `def compute(ohlcv) -> pd.Series`")
    lines.append("- 返回 Series, index=symbol, name=因子名 (或 None, 沙箱会改名)")
    lines.append("- 横截面日数据: 输入是 long-format 多 symbol 多 date")
    lines.append("")
    lines.append("**可参考的横截面 pivot:**")
    lines.append("```python")
    lines.append("wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()")
    lines.append("factor = wide.iloc[-1]  # 取最新一日横截面")
    lines.append("```")
    lines.append("")

    # 2) 当前已上线因子
    lines.append("# 当前已上线因子 (registry)")
    lines.append("")
    lines.append("| name | direction | latest IC | latest IR |")
    lines.append("|---|---|---|---|")
    for f in registry.list_all():
        latest = evaluator.get_latest(f.name, f.factor_version) if evaluator else None
        ic = f"{latest.ic_mean:+.3f}" if latest and latest.ic_mean is not None else "—"
        ir = f"{latest.ir:+.3f}" if latest and latest.ir is not None else "—"
        lines.append(f"| {f.name} | {f.direction} | {ic} | {ir} |")
    lines.append("")

    # 3) 历史 code 因子统计
    code_recent = store.list_recent(limit=200, recipe_kind="code")
    if code_recent:
        total = len(code_recent)
        rej = sum(1 for p in code_recent if p.status == "rejected")
        acc = sum(1 for p in code_recent if p.status in ("accepted", "shadow"))
        pending = sum(1 for p in code_recent if p.status == "llm_suggested")
        lines.append("# 历史 code 因子统计 (最近 200 条)")
        lines.append("")
        lines.append(f"- total={total}, accepted/shadow={acc}, llm_suggested(待审)={pending}, rejected={rej}")
        lines.append("")

        # 4) 最近 10 个 code 因子 — 展示 hash + 描述, 让 LLM 避开
        lines.append("# 最近 10 个 code 因子 (按避免重复)")
        lines.append("")
        lines.append("| hash(前 8) | direction | status | description |")
        lines.append("|---|---|---|---|")
        for p in code_recent[:10]:
            desc = (p.reason or "")[:80].replace("|", "/")
            lines.append(f"| {p.code_hash[:8] if p.code_hash else '?'} | {p.direction} | {p.status} | {desc} |")
        lines.append("")
    else:
        lines.append("# 历史 code 因子统计")
        lines.append("")
        lines.append("(库内尚无 code 因子, 这是首批探索.)")
        lines.append("")

    # 5) DSL 因子的最近 rejection (LLM 可借鉴: DSL 走不通的逻辑可能用 code 走通)
    dsl_recent_rej = [
        p for p in store.list_recent(limit=50, recipe_kind="dsl")
        if p.status == "rejected" and p.reason
    ]
    if dsl_recent_rej:
        lines.append("# 最近 DSL 因子被拒原因 (借鉴 — 用 code 可能实现出来)")
        lines.append("")
        for p in dsl_recent_rej[:8]:
            lines.append(f"- `{p.factor_name}`: {(p.reason or '')[:100]}")
        lines.append("")

    return "\n".join(lines)


# -------------------- LLM 输出解析 --------------------


def _parse_llm_response(text: str) -> list[dict]:
    """从 LLM 返回里提取 suggestions 列表. 兼容截断 (同 llm_dsl_brainstorm)."""
    i = text.find("{")
    j = text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
    try:
        data = json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        # 截断兜底: 从 suggestions 数组起逐个提取
        arr_start = text.find('"suggestions"')
        if arr_start < 0:
            raise
        bracket = text.find("[", arr_start)
        if bracket < 0:
            raise
        arr_end = text.rfind("]")
        arr_body = text[bracket + 1 : arr_end if arr_end > bracket else len(text)]
        suggestions = _extract_complete_objects(arr_body)
        if not suggestions:
            raise
        logger.warning(
            "LLM code brainstorm response truncated; recovered %d complete suggestions via fallback parser",
            len(suggestions),
        )
        return suggestions
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        raise ValueError(f"LLM output missing 'suggestions' list: keys={list(data.keys())}")
    return suggestions


def _extract_complete_objects(arr_body: str) -> list[dict]:
    """截断兜底: 逐个提取完整 {...} 对象."""
    out: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for k, ch in enumerate(arr_body):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(arr_body[start:k + 1]))
                except Exception:  # noqa: BLE001
                    pass
                start = -1
    return out


# -------------------- Brainstormer --------------------


class LLMCodeFactorBrainstormer:
    """让 LLM 自由出 Python compute 代码 → sandbox 编译 → 写入 factor_proposals."""

    def __init__(
        self,
        *,
        llm_client: Any,
        proposal_store: FactorProposalStore,
        registry: Any,
        evaluator: Any,
        repo: Any | None = None,
        model: str,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        timeout_s: int = 90,
        sandbox_timeout_s: float = 10.0,
    ) -> None:
        self._llm = llm_client
        self._store = proposal_store
        self._registry = registry
        self._evaluator = evaluator
        # M19 兼容: 让 brainstormer 入库后能跑 90 天 IS-IC backfill
        self._repo = repo
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._sandbox_timeout_s = sandbox_timeout_s
        self._system_prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 没有 prompt 文件时给个保底 — 不应发生 (bootstrap 应有)
            logger.warning("LLMCodeFactorBrainstormer: prompt file not found at %s", _PROMPT_PATH)
            return "You are a quantitative factor researcher. Output JSON with 'suggestions' list."

    def run(self, *, n: int) -> dict[str, int]:
        """执行一次 brainstorm, 返回 stats."""
        context = build_state_summary(self._registry, self._evaluator, self._store)
        user_msg = (
            f"{context}\n\n---\n\n请给出 **{n}** 个新候选代码因子。\n"
            f"严格 JSON 输出，no extra text。"
        )

        stats: dict[str, int] = {
            "requested": n,
            "accepted_into_review": 0,
            "compile_failed": 0,
            "unsafe_code": 0,
            "timeout": 0,
            "duplicate": 0,
            "duplicate_by_value": 0,  # P1-3: 计算值与已有因子 spearman > threshold
            "invalid": 0,
            "errors": 0,
        }
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
            logger.exception("LLM code brainstorm failed: %s", exc)
            stats["errors"] += 1
            return stats

        # P1-3: 预备冗余检查上下文 - 一次性 build 现有活因子的 factor_history
        # 值级冗余: LLM 常写出'代码不同但计算值等价'的因子 (如 llm_pct_change_close_60 与 momentum_60
        # corr=+1.0), 这些走 code_hash 去重查不出, 会浪费 30s IS-IC backfill.
        # 在入库前用 spearman 相关性检查一次, |corr|>0.85 就当'值级重复'跳过.
        active_hist_map = self._build_active_factor_history()  # 可能失败, None 表示跳过冗余检查
        redundancy_threshold = 0.85

        ts = now_iso()
        for s in suggestions[:n]:
            if not isinstance(s, dict):
                stats["invalid"] += 1
                continue
            source = s.get("source_code")
            direction = (s.get("direction") or "long").lower()
            description = s.get("description") or ""
            if not isinstance(source, str) or not source.strip():
                stats["invalid"] += 1
                continue
            if direction not in ("long", "short"):
                direction = "long"

            # 1) 编译
            try:
                fn, ch = compile_code_factor(source, timeout_s=self._sandbox_timeout_s)
            except UnsafeCodeError as exc:
                logger.info("LLM code rejected (unsafe): %s", exc)
                stats["unsafe_code"] += 1
                continue
            except CodeTimeoutError as exc:
                logger.info("LLM code rejected (smoke-test timeout): %s", exc)
                stats["timeout"] += 1
                continue
            except SyntaxError as exc:
                logger.info("LLM code rejected (syntax): %s", exc)
                stats["compile_failed"] += 1
                continue
            except Exception as exc:  # noqa: BLE001
                logger.info("LLM code rejected (compile error): %s", exc)
                stats["compile_failed"] += 1
                continue

            # 2) 跨 session 去重 (code_hash) - 代码级
            if self._store.exists_code_hash(ch) is not None:
                logger.info("LLM code duplicate (code_hash=%s)", ch[:8])
                stats["duplicate"] += 1
                continue

            # 3) 起名: code_{op_concept}_{hash6}, op_concept 用 description 前缀拼
            concept = (description or "x").lower().replace(" ", "_")[:20]
            concept = "".join(c for c in concept if c.isalnum() or c == "_") or "x"
            name = f"code_{concept}_{ch[:6]}"

            # 3.5) P1-3: 值级冗余检查 (与现有活因子对比)
            if active_hist_map:
                is_redundant, worst_corr, worst_peer = self._is_redundant_by_value(
                    fn, direction, name, active_hist_map, redundancy_threshold,
                )
                if is_redundant:
                    logger.info(
                        "LLM code redundant-by-value: %s ~ %s (spearman=%.3f > %.2f)",
                        name, worst_peer, worst_corr, redundancy_threshold,
                    )
                    stats["duplicate_by_value"] += 1
                    continue

            # 4) 入库
            # recipe_json 留个摘要 (description + direction), 真正的 source 在 recipe_code
            recipe_json = json.dumps(
                {"direction": direction, "description": description[:200]},
                sort_keys=True, ensure_ascii=False,
            )
            self._store.upsert(FactorProposal(
                factor_name=name,
                recipe_kind="code",
                recipe_json=recipe_json,
                direction=direction,
                status="llm_suggested",
                ic_mean=None, ic_std=None, ir=None, t_stat=None, max_abs_corr=None,
                reason=f"LLM code: {description[:300]}",
                created_at=ts,
                evaluated_at=None,
                recipe_code=source,
                code_hash=ch,
            ))
            stats["accepted_into_review"] += 1

        # M19 兼容: 入库后批量 backfill 90 天 IS-IC, 让审核界面立刻看到 IC 曲线
        if self._repo is not None and stats["accepted_into_review"] > 0:
            try:
                self._backfill_history_for_new_factors(suggestions[:n], stats)
            except Exception as exc:  # noqa: BLE001
                logger.exception("history backfill after code brainstorm failed: %s", exc)
                stats["backfill_failed"] = stats.get("backfill_failed", 0) + 1

        return stats

    def _backfill_history_for_new_factors(
        self,
        suggestions: list[dict],
        stats: dict[str, int],
    ) -> None:
        """对刚入库的 code 因子批量跑 90 天 IS-IC 写 factor_metrics + 同步 factor_proposals."""
        from akq_agents.services.factors.history_backfill import (
            HistoryBackfillContext,
            backfill_one,
        )

        # 1) 一次 build ctx
        ctx = HistoryBackfillContext.build(
            repo=self._repo, evaluator=self._evaluator, days=90, step=1,
        )
        if ctx is None:
            logger.warning("code backfill skipped: ctx build failed (no data?)")
            stats["backfill_skipped"] = stats.get("backfill_skipped", 0) + 1
            return

        # 2) 收集刚入库 factor 实例
        n_backfilled = 0
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            source = s.get("source_code")
            description = s.get("description") or ""
            direction = (s.get("direction") or "long").lower()
            if not isinstance(source, str) or not source.strip():
                continue
            try:
                fn, ch = compile_code_factor(source, timeout_s=self._sandbox_timeout_s)
            except Exception:  # noqa: BLE001
                continue
            # 找刚入库的那条 (可能因 dup 跳过)
            if self._store.exists_code_hash(ch) is None:
                continue
            # 构造 CodeFactor (用 db 里的 name)
            from akq_agents.services.factors.base import CodeFactor
            stored = self._store.list_recent(limit=10, recipe_kind="code")
            stored_match = next(
                (p for p in stored if p.code_hash == ch and p.status == "llm_suggested"),
                None,
            )
            if stored_match is None:
                continue
            factor = CodeFactor(
                name=stored_match.factor_name,
                source_code=source,
                fn=fn,
                factor_version=1,
                direction=direction,
                code_hash=ch,
                description=description,
            )
            try:
                result = backfill_one(
                    factor, ctx,
                    evaluator=self._evaluator,
                    proposal_store=self._store,
                )
                if result.get("ok"):
                    n_backfilled += 1
                    logger.info(
                        "backfilled code %s: %d rows, latest_ir=%.3f",
                        stored_match.factor_name, result["n_metrics_written"],
                        result["latest_ir"] or 0.0,
                    )
                else:
                    logger.warning("backfill_one(%s) skipped: %s",
                                   stored_match.factor_name, result.get("reason"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("backfill_one(%s) failed: %s", stored_match.factor_name, exc)
        stats["backfilled"] = n_backfilled

    # ---------------------------------------------------------------- P1-3: 值级冗余检查
    def _build_active_factor_history(self) -> dict[str, Any] | None:
        """一次性算所有 active 因子的 factor_history (dict[name -> DataFrame]).

        返回 None 表示 ctx 或数据不可用, 冗余检查降级跳过 (不影响原有 code_hash 去重).
        缓存到 self._active_hist_cache 让多次 brainstorm 之间不重算 (5min TTL).
        """
        if self._repo is None:
            return None
        try:
            from akq_agents.services.factors.history_backfill import HistoryBackfillContext
            from akq_agents.services.factors.history_backfill import (
                _default_compute_factor_history as _compute_hist,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("redundancy check import failed: %s", exc)
            return None

        ctx = HistoryBackfillContext.build(
            repo=self._repo, evaluator=self._evaluator, days=90, step=1,
        )
        if ctx is None:
            logger.debug("redundancy check ctx build failed")
            return None

        active_map: dict[str, Any] = {}
        # registry.list_all() 是当前活跃 (builtin + accepted) 因子, 不含 shadow/rejected
        try:
            for f in self._registry.list_all():
                try:
                    hist = _compute_hist(f, ctx.ohlcv, ctx.close.index)
                    if hist is not None and not hist.empty:
                        active_map[f.name] = hist
                except Exception:
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("redundancy check active_map build failed: %s", exc)
            return None
        # 保留 self._compute_hist 供候选因子计算使用
        self._compute_hist_fn = _compute_hist
        self._ctx_for_redundancy = ctx
        return active_map

    def _is_redundant_by_value(
        self,
        fn: Any,
        direction: str,
        name: str,
        active_hist_map: dict[str, Any],
        threshold: float,
    ) -> tuple[bool, float, str]:
        """算候选因子 factor_history, 与现有 active 因子取 max |spearman|.

        Returns (is_redundant, worst_corr, worst_peer_name).
        """
        try:
            from akq_agents.services.factors.base import CodeFactor
            from akq_agents.services.factors.discovery import DiscoveryEngine
        except Exception as exc:  # noqa: BLE001
            logger.debug("redundancy check discovery import failed: %s", exc)
            return False, 0.0, ""
        try:
            factor = CodeFactor(
                name=name, source_code="",  # source 不影响 compute (fn 已传)
                fn=fn, factor_version=1, direction=direction,
                code_hash="temp", description="temp",
            )
            hist = self._compute_hist_fn(
                factor, self._ctx_for_redundancy.ohlcv,
                self._ctx_for_redundancy.close.index,
            )
            if hist is None or hist.empty:
                return False, 0.0, ""
            # 全量 vs 所有 active 找最大, 顺便记 worst peer
            worst_corr = 0.0
            worst_peer = ""
            for peer_name, peer_hist in active_hist_map.items():
                c = DiscoveryEngine._max_abs_corr(hist, {peer_name: peer_hist})
                if c is not None and abs(c) > abs(worst_corr):
                    worst_corr = c
                    worst_peer = peer_name
            return abs(worst_corr) > threshold, worst_corr, worst_peer
        except Exception as exc:  # noqa: BLE001
            logger.debug("redundancy check compute failed for %s: %s", name, exc)
            return False, 0.0, ""
