"""C3 金丝雀回测：锁定 backtester 行为，防止 +56% 灾难重现。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from akq_agents.services.portfolio.backtester import (
    BacktestConfig,
    PortfolioBacktester,
)


def _make_close(start: date, prices_per_symbol: dict[str, list[float]]) -> pd.DataFrame:
    """构造 backtester 期望的 close wide table（index 是 datetime.date）。"""
    n = len(next(iter(prices_per_symbol.values())))
    # 简单连续日期（不区分交易/非交易日，单测用）
    dates = [date.fromordinal(start.toordinal() + i) for i in range(n)]
    return pd.DataFrame(prices_per_symbol, index=dates)


def test_backtester_canary_5d_compound_1pct(tmp_path: Path) -> None:
    """5 日单只票每日涨 1%，预期 nav = 1.01^4 ≈ 1.0406。

    C3 历史症状: 同样的简单场景在 bug 期能算成 +56%/+57% 单日跳变。
    这个金丝雀锁定: 任何未来 backtester 算法改动都不能让简单复利输出
    脱离已知正确值。
    """
    close = _make_close(
        date(2026, 1, 5),
        {
            "000001": [10.0, 10.1, 10.201, 10.30301, 10.40604],
            "000300": [3000.0, 3000.0, 3000.0, 3000.0, 3000.0],
        },
    )

    weights_by_date = {"2026-01-05": {"000001": 1.0}}

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, benchmark_symbol="000300"),
    )
    nav_df = bt._replay(weights_by_date, close)

    assert len(nav_df) == 5, f"应输出 5 日 nav，实际 {len(nav_df)}"
    final_nav = float(nav_df.iloc[-1]["nav_net"])
    expected = 1.01 ** 4  # 4 个 +1% 增长日（首日 rebalance 不产生 return）
    assert abs(final_nav - expected) < 0.001, (
        f"金丝雀失败: 期望 nav={expected:.4f}（5 日复利 1%），"
        f"实际 nav={final_nav:.4f}（如果显著超过期望，说明 backtester 算法又坏了，"
        f"参考 C3 bug 历史症状: portfolio_nav 6/18 +56%, 6/22 +57%）"
    )

    # 单日 return 不应该超过 5%（防止 C3 那种 +56% 重现）
    max_daily = float(nav_df["daily_return_net"].abs().max())
    assert max_daily < 0.05, (
        f"单日 |daily_return| {max_daily*100:.1f}% > 5%，不合理。"
        f"5 日每日 1%，理论上单日 return 应该 ≤ 1.5%"
    )


def test_backtester_canary_no_change_keeps_nav_at_1(tmp_path: Path) -> None:
    """3 日横盘，nav 应保持 1.0（无变化、无 cost）。"""
    close = _make_close(
        date(2026, 2, 3),
        {
            "000001": [10.0, 10.0, 10.0],
            "000300": [3000.0, 3000.0, 3000.0],
        },
    )
    weights_by_date = {"2026-02-03": {"000001": 1.0}}

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0),
    )
    nav_df = bt._replay(weights_by_date, close)

    # 3 日 nav 全部 = 1.0
    for i, row in nav_df.iterrows():
        assert abs(float(row["nav_net"]) - 1.0) < 1e-9, f"day {i}: nav={row['nav_net']}（应保持 1.0）"
