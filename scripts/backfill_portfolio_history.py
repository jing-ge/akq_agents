"""批量回填历史 portfolio_snapshots（用于 backtester 出曲线）。

对过去 N 个交易日，每天调用 PortfolioAgent._run_p3 生成 snapshot 写库。
"""

import sys
sys.path.insert(0, "src")

from datetime import date, timedelta

from akq_agents.bootstrap import build_workflow
from akq_agents.agents.base import AgentContext
from akq_agents.agents.portfolio_agent import PortfolioAgent


def main(n_days: int = 60, end: date | None = None) -> None:
    wf, cfg = build_workflow()
    services = wf.services
    end = end or date.today()

    # 拿 calendar 真实交易日
    cal = services["data_repository"]._calendar
    all_days = cal._days_sorted
    trading_days = [d for d in all_days if d <= end][-n_days:]
    print(f"将回填 {len(trading_days)} 个交易日：{trading_days[0]} ~ {trading_days[-1]}")

    agent = PortfolioAgent(cfg.research.top_n_symbols, services=services)
    ok = 0
    skipped = 0
    for i, td in enumerate(trading_days):
        ctx = AgentContext(state={"today": td.isoformat()})
        # 必须先填 factor_scores 等吗？P3 路径走 repo + factor_engine，不用 context.state["factor_scores"]
        result = agent.run(ctx)
        status = result.get("status") if isinstance(result, dict) else None
        print(f"  [{i+1}/{len(trading_days)}] {td} → {status} size={result.get('portfolio_size')}")
        if status == "ok":
            ok += 1
        else:
            skipped += 1
    print(f"\n回填完成：ok={ok}  skipped={skipped}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    main(n_days=n)
