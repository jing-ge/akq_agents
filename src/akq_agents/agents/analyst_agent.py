"""AnalystAgent v2（P4）：盘后离线生成 markdown 简评，**不使用 ToolUse**。

设计要点（spec v2）：
- 上下文已在 ``context.state`` 中（portfolio / attribution / data_health / events）
- LLM 直接拿这些数据"写文章"；不需要工具循环
- 上下文裁剪：top 20 持仓 + portfolio_contribution + 关键 health 字段 + 近 10 条 events
- LLM 失败 → 退化到 AdvisorAgent 模板渲染（旧实现），不阻塞 batch
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.llm_config import AnalystSubConfig, SafetyConfig
from akq_agents.services.llm.client import LLMGatewayError
from akq_agents.services.llm.orchestrator import LLMOrchestrator

logger = logging.getLogger(__name__)


_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "analyst.md"


class AnalystAgent(BaseAgent):
    name = "analyst-agent"

    def __init__(
        self,
        orchestrator: LLMOrchestrator,
        cfg: AnalystSubConfig,
        reports_dir: Path,
        safety: SafetyConfig,
        events_fetch: Any | None = None,
    ) -> None:
        """
        Args:
            orchestrator: 已装配的 LLMOrchestrator
            cfg: AnalystSubConfig（model / max_tokens / temperature / context_*）
            reports_dir: 报告输出目录
            safety: SafetyConfig（disclaimer header）
            events_fetch: 可选 events 来源；签名 ``() -> list[dict]``。
                如未提供，则不在 prompt 中塞 events 列表。
        """
        self._orch = orchestrator
        self._cfg = cfg
        self._reports_dir = Path(reports_dir)
        self._safety = safety
        self._events_fetch = events_fetch

    def run(self, context: AgentContext) -> dict[str, Any]:
        if not self._cfg.enabled:
            return {"status": "skipped", "reason": "disabled"}

        today = self._get_today(context)
        try:
            payload = self._build_context_payload(context)
        except Exception as exc:  # noqa: BLE001 — 构造上下文失败也要 fallback
            logger.exception("analyst context build failed: %s", exc)
            return self._fallback(context, reason="context_build_failed")

        system_prompt = _load_prompt() + "\n\n" + self._safety.disclaimer_header
        user_message = self._render_user(payload)

        try:
            text = self._orch.run_analyst(
                session_id=f"analyst:{today.isoformat()}",
                system_prompt=system_prompt,
                user_message=user_message,
                model=self._cfg.model,
                max_tokens=self._cfg.max_tokens,
                temperature=self._cfg.temperature,
            )
        except LLMGatewayError as exc:
            logger.warning("analyst LLM unavailable, degrading: %s", exc)
            return self._fallback(context, reason=f"llm_{exc.reason_code.lower()}")

        path = self._write_report(today, text)
        return {"status": "ok", "path": str(path), "chars": len(text)}

    # ----------------- internal helpers -----------------

    @staticmethod
    def _get_today(context: AgentContext) -> date:
        today = context.state.get("today")
        if today is None:
            return date.today()
        if isinstance(today, str):
            return date.fromisoformat(today)
        return today  # 已是 date

    def _build_context_payload(self, context: AgentContext) -> dict[str, Any]:
        portfolio = list(context.state.get("portfolio") or [])
        # 取 top N by weight
        portfolio_sorted = sorted(
            portfolio,
            key=lambda r: float(r.get("weight", 0.0)) or 0.0,
            reverse=True,
        )[: self._cfg.context_top_holdings]

        attribution = context.state.get("attribution") or {}
        port_contrib = attribution.get("portfolio_contribution", {}) if isinstance(attribution, dict) else {}

        data_health = self._summarize_health(context.state.get("data_health"))

        events: list[dict[str, Any]] = []
        if self._events_fetch is not None:
            try:
                events = list(self._events_fetch() or [])
            except Exception:  # noqa: BLE001
                events = []
        events = events[: self._cfg.context_events_count]

        return {
            "as_of_date": self._get_today(context).isoformat(),
            "portfolio_top": portfolio_sorted,
            "portfolio_n": len(portfolio),
            "turnover": context.state.get("portfolio_turnover"),
            "portfolio_contribution": port_contrib,
            "data_health": data_health,
            "events": events,
        }

    @staticmethod
    def _summarize_health(raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return {
                k: raw.get(k)
                for k in (
                    "last_full_refresh",
                    "universe_size_today",
                    "ohlcv_coverage_today",
                    "pending_retries",
                    "unresolved_errors_24h",
                    "health",
                )
                if k in raw
            }
        # 可能是 DataHealth pydantic 模型
        try:
            return raw.model_dump(mode="json")  # type: ignore[no-any-return]
        except AttributeError:
            return {}

    @staticmethod
    def _render_user(payload: dict[str, Any]) -> str:
        return (
            "请根据以下盘后上下文写一份简评：\n\n```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n```"
        )

    def _write_report(self, today: date, text: str) -> Path:
        directory = self._reports_dir / today.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "analyst_brief.md"
        # 首行硬注入 disclaimer
        content = f"> {self._safety.disclaimer_header}\n\n" + text.lstrip()
        path.write_text(content, encoding="utf-8")
        return path

    def _fallback(self, context: AgentContext, *, reason: str) -> dict[str, Any]:
        """LLM 失败 → 用 context 已有 advice / portfolio 拼一个模板报告。"""
        today = self._get_today(context)
        advice = context.state.get("daily_advice") or {}
        rendered = advice.get("rendered") if isinstance(advice, dict) else ""
        portfolio_n = len(context.state.get("portfolio") or [])

        body_lines = [
            f"> {self._safety.disclaimer_header}",
            "",
            "## 数据状态",
            f"- 持仓数：{portfolio_n}",
            f"- 退化原因：`{reason}`（LLM 不可用，使用模板版本）",
            "",
            "## 组合概览（模板版本）",
            rendered.strip() if isinstance(rendered, str) and rendered.strip() else "_暂无 advice 输出_",
        ]
        text = "\n".join(body_lines)
        directory = self._reports_dir / today.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "analyst_brief.md"
        path.write_text(text, encoding="utf-8")
        return {"status": "degraded", "reason": reason, "path": str(path)}


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")
