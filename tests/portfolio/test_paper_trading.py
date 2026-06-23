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
