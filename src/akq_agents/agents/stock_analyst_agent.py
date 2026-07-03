"""个股 AI 分析 agent。

给 ``/api/stock/analyze/{symbol}`` endpoint 提供 LLM 调用封装：

1. 从 :class:`StockDetailService` 拿 overview + 最近 60 日 K 线
2. 后端计算 MA / MACD 最新值（不能依赖前端）
3. 组装严格约束的 system prompt + 结构化 user_message
4. 调用 ``LLMOrchestrator.run_analyst()`` 单轮同步返回
5. 显式往 :class:`LLMStore` 写 system / user / assistant 三行 message，
   让后续追问（走 ``/api/chat/sessions/{sid}/messages`` 复用 chat 通路）能读到

**约束**：本 agent **不** call tools，直接把数据打包进 prompt 一次性给 LLM。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from akq_agents.services.stock_detail import (
    StockDetailService,
    compute_ma,
    compute_macd,
)

logger = logging.getLogger(__name__)


_STOCK_ANALYST_SYSTEM_PROMPT = """你是一名 A 股基本面 + 技术面综合分析师，只做研究性分析，不构成投资建议。

**必须严格遵守：**
1. 输出必须**恰好**包含 4 个二级标题（Markdown ##）：`## 技术面` / `## 量价` / `## 估值` / `## 风险提示`。顺序固定。
2. 全部中文输出。**禁止使用**「买入 / 卖出 / 加仓 / 减仓 / 目标价 / 止损位 / 建议持有 / 值得关注」等操作性字眼。
3. 每段 3-6 句，每句要有具体数据支撑（引用 user_message 里给到的数字，例如"MA5=7.56, MA30=8.54"）。
4. 不做未来价格预测，只描述当前形态与历史类比。
5. 若某类数据缺失（user 会显式列出 degraded_fields），该段用"数据缺失，无法评估"一句带过，不要编造。
6. 结尾**不要**自己写免责声明，系统会自动追加。
"""


def analyze(
    *,
    symbol: str,
    detail_service: StockDetailService,
    llm_orchestrator: Any,
    llm_config: Any,
    llm_store: Any,
    period_context: str = "D",
    disclaimer_header: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """执行一次同步 AI 分析。

    :returns: ``{content, session_id, model, prompt_tokens, completion_tokens, as_of}``
    :raises: ``LLMGatewayError`` 若上游失败（不 swallow，让 endpoint 转 502）
    """
    # 1) 拉数据
    overview = detail_service.fetch_overview(symbol)
    kline = detail_service.fetch_kline(symbol, "D", limit=60)

    # 2) 后端算 MA / MACD 最新值
    closes = [b["c"] for b in kline["bars"] if b.get("c") is not None]
    ma_snapshot: dict[str, float | None] = {}
    for w in (5, 10, 30, 60):
        series = compute_ma(closes, w)
        ma_snapshot[f"MA{w}"] = series[-1] if series else None
    dif, dea, hist = compute_macd(closes)
    macd_snapshot = {
        "DIF": dif[-1] if dif else None,
        "DEA": dea[-1] if dea else None,
        "MACD": hist[-1] if hist else None,
    }

    # 3) 组装 user_message
    user_message = _build_user_message(overview, kline, ma_snapshot, macd_snapshot, period_context)

    # 4) 组装 system prompt（带 disclaimer 尾巴给 assistant 的存档参考，run_analyst 只把这一段作为 system）
    system_prompt = _STOCK_ANALYST_SYSTEM_PROMPT
    if disclaimer_header:
        system_prompt = system_prompt + "\n\n【系统免责】\n" + disclaimer_header

    # 5) 调 LLM
    session_id = f"stock:{symbol}:{uuid.uuid4().hex[:8]}"
    model = model_override or getattr(llm_config.chat, "model", None) or getattr(llm_config, "default_model", "Claude-Opus-4.7")
    max_tokens = getattr(llm_config.chat, "max_tokens", 2000)
    temperature = getattr(llm_config.chat, "temperature", 0.4)

    # 记录 session 三行消息（system + user + assistant），让追问 endpoint 能读到
    if llm_store is not None:
        llm_store.append_message(session_id=session_id, role="system", content=system_prompt)
        llm_store.append_message(session_id=session_id, role="user", content=user_message)

    text = llm_orchestrator.run_analyst(
        session_id=session_id,
        system_prompt=system_prompt,
        user_message=user_message,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=60,
    )

    # 追加免责尾巴（LLM 已被禁止自写，此处系统统一贴）
    final_content = text.rstrip()
    if disclaimer_header:
        final_content = final_content + "\n\n---\n" + disclaimer_header

    if llm_store is not None:
        llm_store.append_message(session_id=session_id, role="assistant", content=final_content)

    return {
        "symbol": symbol,
        "content": final_content,
        "session_id": session_id,
        "model": model,
        "as_of": datetime.now().isoformat(timespec="seconds"),
    }


def _build_user_message(
    overview: Any, kline: dict[str, Any], ma: dict[str, Any], macd: dict[str, Any], period_context: str
) -> str:
    """把结构化数据序列化为 markdown / 键值对形式的 prompt。"""
    q = overview.quote or {}
    lines: list[str] = []
    lines.append(f"请你基于以下数据对 A 股 **{overview.name or '未知'}（{overview.symbol}）** 出四段研究性分析。用户当前查看的 K 线周期：{period_context}。")
    lines.append("")
    lines.append("## 一、基本信息")
    lines.append(f"- 所属行业：{overview.industry or '数据缺失'}")
    lines.append(f"- 行业当日涨跌幅：{_fmt_pct(overview.industry_pct_change)}")
    lines.append(f"- 总市值：{_fmt_yi(overview.market_cap)}")
    lines.append(f"- 市盈率（PE）：{_fmt_num(overview.pe_ratio)}")
    lines.append(f"- 市净率（PB）：{_fmt_num(overview.pb_ratio)}")
    lines.append(f"- 上市日期：{overview.listing_date or '数据缺失'}")
    lines.append("")
    lines.append("## 二、当日行情")
    lines.append(f"- 现价：{_fmt_num(q.get('price'))}")
    lines.append(f"- 涨跌幅：{_fmt_pct(q.get('pct_change'))}")
    lines.append(f"- 今开：{_fmt_num(q.get('open'))} / 昨收：{_fmt_num(q.get('prev_close'))}")
    lines.append(f"- 最高：{_fmt_num(q.get('high'))} / 最低：{_fmt_num(q.get('low'))}")
    lines.append(f"- 成交量：{_fmt_wan(q.get('volume'))}手 / 成交额：{_fmt_yi(q.get('amount'))}")
    lines.append(f"- 换手率：{_fmt_pct(q.get('turnover_ratio'))}")
    lines.append(f"- 至今涨跌幅：{_fmt_pct(q.get('since_listing_pct'))}（自本地可查最早交易日起）")
    lines.append("")
    lines.append("## 三、技术指标（最新一根日 K）")
    lines.append(f"- MA5：{_fmt_num(ma.get('MA5'))} / MA10：{_fmt_num(ma.get('MA10'))}")
    lines.append(f"- MA30：{_fmt_num(ma.get('MA30'))} / MA60：{_fmt_num(ma.get('MA60'))}")
    lines.append(f"- MACD（柱）：{_fmt_num(macd.get('MACD'))} / DIF：{_fmt_num(macd.get('DIF'))} / DEA：{_fmt_num(macd.get('DEA'))}")
    lines.append("")
    lines.append("## 四、近 30 交易日 K 线（open / close / low / high / volume）")
    tail = kline["bars"][-30:] if kline.get("bars") else []
    for b in tail:
        lines.append(
            f"- {b.get('t')}: o={_fmt_num(b.get('o'))}, c={_fmt_num(b.get('c'))}, "
            f"l={_fmt_num(b.get('l'))}, h={_fmt_num(b.get('h'))}, v={_fmt_wan(b.get('v'))}手"
        )
    if not tail:
        lines.append("- 本地无 K 线数据（degraded）")
    if overview.degraded_fields:
        lines.append("")
        lines.append(f"> **数据完整性提示**：以下字段本次拉取失败，请在对应章节声明数据缺失：{', '.join(overview.degraded_fields)}")
    lines.append("")
    lines.append("请严格按 `## 技术面` / `## 量价` / `## 估值` / `## 风险提示` 四个二级标题输出。")
    return "\n".join(lines)


# ---------------------------------------------------------------- formatters


def _fmt_num(v: Any) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "-"
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    return f"{f:.4f}".rstrip("0").rstrip(".")


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f}%"
    except (ValueError, TypeError):
        return "-"


def _fmt_yi(v: Any) -> str:
    """把大额金额转换成"X.XX 亿"。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "-"
    if abs(f) >= 1e8:
        return f"{f / 1e8:.2f}亿"
    if abs(f) >= 1e4:
        return f"{f / 1e4:.2f}万"
    return f"{f:.2f}"


def _fmt_wan(v: Any) -> str:
    """把大额数量转换成"X.XX 万手"。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "-"
    if abs(f) >= 1e4:
        return f"{f / 1e4:.2f}万"
    return f"{f:.0f}"
