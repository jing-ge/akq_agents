"""批量回填 portfolio_snapshots（1 年 = 250 个交易日）。

为了避免每天都 rebuild NAV（O(N²) 复杂度），传 services 时去掉 portfolio_backtester。
完成后再一次性算 NAV。
"""

import sys
sys.path.insert(0, "src")

from datetime import date, timedelta
import time

from akq_agents.bootstrap import build_workflow
from akq_agents.agents.base import AgentContext
from akq_agents.agents.portfolio_agent import PortfolioAgent


def main(n_days: int | None = None, end: date | None = None) -> None:
    wf, cfg = build_workflow()
    services = dict(wf.services)  # 浅复制
    # 关键：临时去掉 backtester，避免 N 次 O(N) NAV 重算
    services.pop("portfolio_backtester", None)

    end = end or date.today()
    cal = wf.services["data_repository"]._calendar
    all_days = cal._days_sorted
    trading_days = [d for d in all_days if d <= end]
    if n_days is not None:
        trading_days = trading_days[-n_days:]
    print(f"将回填 {len(trading_days)} 个交易日：{trading_days[0]} ~ {trading_days[-1]}")

    agent = PortfolioAgent(cfg.research.top_n_symbols, services=services)
    ok = 0
    skipped = 0
    start_t = time.monotonic()
    for i, td in enumerate(trading_days):
        ctx = AgentContext(state={"today": td.isoformat()})
        result = agent.run(ctx)
        status = result.get("status") if isinstance(result, dict) else None
        if status == "ok":
            ok += 1
        else:
            skipped += 1
        if (i + 1) % 20 == 0 or i == len(trading_days) - 1:
            elapsed = time.monotonic() - start_t
            eta = elapsed / (i + 1) * (len(trading_days) - i - 1)
            print(f"  [{i+1}/{len(trading_days)}] {td}  ok={ok} skipped={skipped}  ({elapsed:.1f}s elapsed, ETA {eta:.0f}s)")

    elapsed = time.monotonic() - start_t
    print(f"\n回填完成：ok={ok}  skipped={skipped}  耗时 {elapsed:.1f}s")

    # 一次性算 NAV
    bt = wf.services.get("portfolio_backtester")
    if bt is not None and ok > 1:
        print("\n=== 全量重算 NAV ===")
        start_t = time.monotonic()
        result = bt.rebuild_full_history()
        elapsed = time.monotonic() - start_t
        print(f"NAV 重算完成（{elapsed:.1f}s）：")
        import json; print(json.dumps(result.summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    main(n_days=n if n > 0 else None)
