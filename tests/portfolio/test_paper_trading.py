from __future__ import annotations

import sqlite3
from datetime import date

from akq_agents.services.portfolio.paper_trading import PaperTradingStore


def test_update_track_perf_uses_lookup_when_close_missing(tmp_path):
    """C4 regression: 估值缺价时优先 lookup 最近有效 close，不能直接 fallback frozen_price。"""
    store = PaperTradingStore(tmp_path / "meta.db")
    cohort_date = date(2026, 1, 1)
    today = date(2026, 6, 23)

    weights = {"000001": 0.5, "000002": 0.5}
    close_at_cohort = {"000001": 10.0, "000002": 10.0}
    store.freeze_today_cohort(cohort_date, weights, close_at_cohort)

    latest_close = {"000002": 12.0, "000300": 3000.0}

    def lookup(symbol, d):
        if symbol == "000001" and d == today:
            return 11.0
        return None

    store.update_track_perf(today, latest_close, cohort_close_lookup=lookup)

    con = sqlite3.connect(tmp_path / "meta.db")
    try:
        row = con.execute(
            "SELECT return_pct FROM paper_track_perf WHERE cohort_date=? AND as_of_date=?",
            (cohort_date.isoformat(), today.isoformat()),
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    return_pct = row[0]
    assert abs(return_pct - 0.15) < 0.001, (
        f"期望 15% (lookup 11.0)，实际 {return_pct * 100:.1f}% "
        "(说明用了 frozen_price=10.0)"
    )


def test_freeze_is_idempotent_no_weight_inflation(tmp_path):
    """守恒 regression: 同一 cohort_date 二次 freeze 不能叠加权重。

    历史 bug (2026-07-02): daemon 当天跑了两次, 第一次冻结 73 票(权重和1.0),
    第二次用不同票池追加 31 票, INSERT OR IGNORE 只按 (cohort,symbol) 去重,
    新票直接追加 → 权重和累积到 1.24, 持仓市值 12.4万 vs 本金 10万,
    该 cohort return_pct 系统性高估 ~24%。

    正确语义: paper trading 锁定当日决策, 一天只冻结一次, 重跑幂等。
    """
    store = PaperTradingStore(tmp_path / "meta.db")
    cohort_date = date(2026, 7, 2)

    # 第一次: 2 票, 权重和 1.0
    store.freeze_today_cohort(
        cohort_date,
        {"000001": 0.5, "000002": 0.5},
        {"000001": 10.0, "000002": 10.0},
    )
    # 第二次: 不同票池 (含 1 只新票), 若叠加权重和会 > 1
    store.freeze_today_cohort(
        cohort_date,
        {"000001": 0.5, "000003": 0.5},
        {"000001": 10.0, "000003": 10.0},
    )

    con = sqlite3.connect(tmp_path / "meta.db")
    try:
        n, wsum = con.execute(
            "SELECT COUNT(*), SUM(frozen_weight) FROM paper_trades WHERE cohort_date=?",
            (cohort_date.isoformat(),),
        ).fetchone()
    finally:
        con.close()

    assert abs(wsum - 1.0) < 1e-9, (
        f"二次 freeze 后权重和={wsum} 应=1.0。>1 说明第二次 freeze 叠加了新票 "
        f"(2026-07-02 权重膨胀 bug), 会让该 cohort 收益系统性高估。"
    )
    assert n == 2, f"应保留首次冻结的 2 票, 实际 {n} 票 (二次 freeze 不应追加新票)"
