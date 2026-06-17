from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from akq_agents.agents.base import AgentContext, BaseAgent


class ReportAgent(BaseAgent):
    name = "report-agent"

    def __init__(self, report_dir: str):
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def run(self, context: AgentContext):
        advice = context.state.get("daily_advice", {})
        selected_factors = context.state.get("selected_factors", [])
        portfolio = context.state.get("portfolio", [])
        research_summary = context.state.get("research_summary", {})
        generated_at = advice.get("generated_at", datetime.now().isoformat())
        stamp = generated_at[:10]
        markdown_path = self.report_dir / f"daily_report_{stamp}.md"
        html_path = self.report_dir / f"daily_report_{stamp}.html"

        markdown_content = self._render_markdown(generated_at, advice, selected_factors, portfolio, research_summary)
        html_content = self._render_html(generated_at, advice, selected_factors, portfolio, research_summary)

        markdown_path.write_text(markdown_content, encoding="utf-8")
        html_path.write_text(html_content, encoding="utf-8")
        context.state["latest_report"] = str(markdown_path)
        context.state["latest_report_html"] = str(html_path)
        return {"report_path": str(markdown_path), "html_report_path": str(html_path)}

    def _render_markdown(self, generated_at, advice, selected_factors, portfolio, research_summary) -> str:
        lines = [
            "# 每日量化日报",
            "",
            f"- 生成时间: {generated_at}",
            f"- 策略摘要: {advice.get('summary', '暂无')}",
            f"- 候选因子数: {research_summary.get('eligible_factor_count', 0)}",
            f"- 入选因子数: {research_summary.get('selected_factor_count', 0)}",
            "",
            "## 入选因子",
        ]
        if selected_factors:
            for item in selected_factors:
                lines.append(
                    f"- {item['factor_name']}: score={item['score']:.4f}, annual_return={item['annual_return']:.4f}, sharpe={item['sharpe']:.4f}, max_drawdown={item['max_drawdown']:.4f}, ic={item.get('ic', 0.0):.4f}"
                )
        else:
            lines.append("- 暂无入选因子")

        lines.extend(["", "## 组合建议"])
        if portfolio:
            for item in portfolio:
                reasons = ", ".join(item.get("reasons", []))
                lines.append(f"- {item['symbol']}: weight={item['weight']:.2%}, score={item['score']:.4f}, reasons={reasons}")
        else:
            lines.append("- 暂无组合建议")

        lines.extend(["", "## 操盘建议"])
        lines.append(f"- 观察池: {', '.join(advice.get('watchlist', [])) or '无'}")
        lines.append(f"- 买入候选: {', '.join(advice.get('buy_candidates', [])) or '无'}")
        lines.append(f"- 减仓候选: {', '.join(advice.get('reduce_candidates', [])) or '无'}")

        lines.extend(["", "## 风险提示"])
        for note in advice.get("risk_notes", []):
            lines.append(f"- {note}")
        if not advice.get("risk_notes"):
            lines.append("- 无")
        return "\n".join(lines) + "\n"

    def _render_html(self, generated_at, advice, selected_factors, portfolio, research_summary) -> str:
        factor_rows = "".join(
            f"<tr><td>{escape(item['factor_name'])}</td><td>{item['score']:.4f}</td><td>{item['annual_return']:.4f}</td><td>{item['sharpe']:.4f}</td><td>{item['max_drawdown']:.4f}</td><td>{item.get('ic', 0.0):.4f}</td></tr>"
            for item in selected_factors
        ) or "<tr><td colspan='6'>暂无入选因子</td></tr>"
        portfolio_rows = "".join(
            f"<tr><td>{escape(item['symbol'])}</td><td>{item['weight']:.2%}</td><td>{item['score']:.4f}</td><td>{escape(', '.join(item.get('reasons', [])))}</td></tr>"
            for item in portfolio
        ) or "<tr><td colspan='4'>暂无组合建议</td></tr>"
        risk_items = "".join(f"<li>{escape(note)}</li>" for note in advice.get("risk_notes", [])) or "<li>无</li>"
        return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>每日量化日报</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; color: #1f2937; }}
    h1, h2 {{ color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 10px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .meta {{ background: #f9fafb; padding: 16px; border-radius: 8px; margin-bottom: 24px; }}
  </style>
</head>
<body>
  <h1>每日量化日报</h1>
  <div class="meta">
    <p><strong>生成时间:</strong> {escape(generated_at)}</p>
    <p><strong>策略摘要:</strong> {escape(advice.get('summary', '暂无'))}</p>
    <p><strong>候选因子数:</strong> {research_summary.get('eligible_factor_count', 0)}</p>
    <p><strong>入选因子数:</strong> {research_summary.get('selected_factor_count', 0)}</p>
    <p><strong>观察池:</strong> {escape(', '.join(advice.get('watchlist', [])) or '无')}</p>
    <p><strong>买入候选:</strong> {escape(', '.join(advice.get('buy_candidates', [])) or '无')}</p>
    <p><strong>减仓候选:</strong> {escape(', '.join(advice.get('reduce_candidates', [])) or '无')}</p>
  </div>
  <h2>入选因子</h2>
  <table>
    <thead><tr><th>因子</th><th>综合分</th><th>年化收益</th><th>夏普</th><th>最大回撤</th><th>IC</th></tr></thead>
    <tbody>{factor_rows}</tbody>
  </table>
  <h2>组合建议</h2>
  <table>
    <thead><tr><th>标的</th><th>权重</th><th>分数</th><th>原因</th></tr></thead>
    <tbody>{portfolio_rows}</tbody>
  </table>
  <h2>风险提示</h2>
  <ul>{risk_items}</ul>
</body>
</html>
""".strip() + "\n"
