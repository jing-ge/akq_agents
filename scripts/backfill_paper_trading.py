"""回填 paper_trades：用 portfolio_snapshots 历史 + 当日 close 模拟"如果每天都按系统建议建仓"。

注意：这是 in-sample 数据，但能给用户提供"过去 192 天每天的推荐如果都执行了今天值多少"
这种**累积视角**，而不是真正的 out-of-sample 验证。

真正的 OOS 验证只能靠**从今天起每天自动冻结**（已经在 portfolio_agent 里挂上了）。
"""

import sys
sys.path.insert(0, "src")

from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds


def main() -> None:
    from akq_agents.bootstrap import build_workflow
    wf, _ = build_workflow()
    repo = wf.services["data_repository"]
    paper = wf.services["paper_trading_store"]

    # 1) 从 portfolio_snapshots 读所有历史 cohort
    import sqlite3
    db = repo._base_dir / "meta.db"
    conn = sqlite3.connect(db)
    cohort_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT as_of_date FROM portfolio_snapshots ORDER BY as_of_date ASC"
    ).fetchall()]
    print(f"从 portfolio_snapshots 找到 {len(cohort_dates)} 个 cohort 日期")
    print(f"  范围: {cohort_dates[0]} ~ {cohort_dates[-1]}")

    # 2) 一次性读所有需要的 close 价格（包括 benchmark）
    print("拉取所有日期的 close...")
    start = date.fromisoformat(cohort_dates[0])
    end = date.fromisoformat(cohort_dates[-1])
    dataset = ds.dataset(repo._ohlcv_dir, format="parquet", partitioning="hive")
    table = dataset.to_table(
        filter=(ds.field("date") >= start.isoformat()) & (ds.field("date") <= end.isoformat()),
        columns=["date", "symbol", "close"],
    )
    all_close = table.to_pandas()
    all_close["date"] = pd.to_datetime(all_close["date"]).dt.date
    print(f"  共 {len(all_close)} 行价格数据")

    # close 按 date 索引方便查
    close_by_date: dict = {}
    for d, group in all_close.groupby("date"):
        close_by_date[d] = dict(zip(group["symbol"].astype(str), group["close"]))

    latest_date = max(close_by_date.keys())
    # 找最新的 benchmark 也有的日期作为 as_of（避免今日 stock_zh_a_spot 拉的没含 benchmark）
    bench_sym = "000300"
    bench_dates = sorted([d for d, m in close_by_date.items() if bench_sym in m], reverse=True)
    if bench_dates:
        latest_date = bench_dates[0]
        print(f"  benchmark 最新有数据的日期: {latest_date}")
    print(f"  最新交易日: {latest_date}")
    latest_close = close_by_date[latest_date]

    # 3) 对每个 cohort 冻结 + 立即用 latest_close 估值
    n_frozen = 0
    n_evaluated = 0
    for cd_str in cohort_dates:
        cd = date.fromisoformat(cd_str)
        # 读这个 cohort 的权重
        rows = conn.execute(
            "SELECT symbol, weight FROM portfolio_snapshots WHERE as_of_date = ?",
            (cd_str,),
        ).fetchall()
        if not rows:
            continue
        weights = {str(s): float(w) for s, w in rows}
        close_that_day = close_by_date.get(cd, {})
        if not close_that_day:
            continue
        n = paper.freeze_today_cohort(cd, weights, close_that_day)
        n_frozen += n if n else len(rows)

    # 4) 用 latest_close 估值所有 cohort（带 benchmark lookup）
    print(f"\n冻结完成：{n_frozen} 个 (cohort, symbol) 对")
    print(f"\n开始估值 {len(cohort_dates)} 个 cohort（用 {latest_date} close）...")

    def cohort_close_lookup(symbol: str, d):
        """从 close_by_date 反查某日的 symbol close。"""
        day_close = close_by_date.get(d, {})
        v = day_close.get(symbol)
        return float(v) if v is not None else None

    stats = paper.update_track_perf(latest_date, latest_close, cohort_close_lookup=cohort_close_lookup)
    print(f"估值完成：{stats}")

    # 5) 报告 summary
    summary = paper.summary()
    print(f"\n=== Paper Trading Summary ===")
    import json
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
