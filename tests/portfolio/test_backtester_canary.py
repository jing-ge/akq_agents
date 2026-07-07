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
    """单只票每日涨 1%，T+1 成交后预期正确复利。

    C3 历史症状: 同样的简单场景在 bug 期能算成 +56%/+57% 单日跳变。
    这个金丝雀锁定: 任何未来 backtester 算法改动都不能让简单复利输出
    脱离已知正确值。

    **T+1 语义**: 信号在 2026-01-05 (day0) 收盘后产生, 2026-01-06 (day1)
    才建仓。建仓后持有到 day4, 期间 day1→day2→day3→day4 共 3 个 +1% 增长日,
    故 final nav = 1.01^3。(day0 无持仓 nav=1.0; day1 建仓当日 nav 仍=1.0)
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

    # T+1: 从 day1 (2026-01-06) 建仓开始输出, 共 4 日 (day1..day4)
    assert len(nav_df) == 4, f"T+1 成交应从信号次日起输出 4 日 nav，实际 {len(nav_df)}"
    final_nav = float(nav_df.iloc[-1]["nav_net"])
    expected = 1.01 ** 3  # T+1 建仓后 3 个 +1% 增长日 (day1建仓→day2/3/4 涨)
    assert abs(final_nav - expected) < 0.001, (
        f"金丝雀失败: 期望 nav={expected:.4f}（T+1 建仓后 3 日复利 1%），"
        f"实际 nav={final_nav:.4f}（如果显著超过期望，说明 backtester 算法又坏了，"
        f"参考 C3 bug 历史症状: portfolio_nav 6/18 +56%, 6/22 +57%）"
    )

    # 单日 return 不应该超过 5%（防止 C3 那种 +56% 重现）
    max_daily = float(nav_df["daily_return_net"].abs().max())
    assert max_daily < 0.05, (
        f"单日 |daily_return| {max_daily*100:.1f}% > 5%，不合理。"
        f"每日 1%，理论上单日 return 应该 ≤ 1.5%"
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


def test_backtester_cost_is_two_sided(tmp_path: Path) -> None:
    """R4 回归：rebalance cost 必须按双边算（buy + sell 都付）。

    场景：单日 rebalance 100% 换手（旧权重 A=1.0 → 新权重 B=1.0），
    单边费率 0.0008（commission 0.0003 + slippage 0.0005），
    期望 cost = 2 × 1.0 × 0.0008 = 0.0016（双边）。
    """
    close = _make_close(
        date(2026, 3, 2),
        {
            "A": [10.0, 10.0, 10.0],
            "B": [20.0, 20.0, 20.0],
            "000300": [3000.0, 3000.0, 3000.0],
        },
    )
    # 第一日全仓 A，第二日全仓 B（100% 换手）
    weights_by_date = {
        "2026-03-02": {"A": 1.0},
        "2026-03-03": {"B": 1.0},
    }
    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0003, slippage=0.0005, benchmark_symbol="000300"),
    )
    nav_df = bt._replay(weights_by_date, close)

    # 第二日 rebalance turnover 应为 1.0 (A↔B 100% 换仓)
    day2 = nav_df.iloc[1]
    assert abs(float(day2["turnover"]) - 1.0) < 1e-9
    # 双边 cost：2 × 1.0 × 0.0008 = 0.0016（如果还是单边 bug 会得到 0.0008）
    assert abs(float(day2["cost"]) - 0.0016) < 1e-9, (
        f"R4 失败：rebalance 100% 换手的 cost 应为 0.0016（双边），实际 {day2['cost']:.6f}"
    )
    # 这里**不**断言 nav_net 的绝对值，因为首日建仓也有 cost；
    # 只断言 day2 单步 cost 含义 + day2 nav 较 day1 下降 ≈ 0.0016


def test_backtester_nav_gross_diverges_from_net_when_cost(tmp_path: Path) -> None:
    """R4 回归：nav_gross 必须独立于 nav_net 计算，反映未扣费收益。

    之前 nav_gross == nav_net 整段时间，gross/net 曲线完全重合，
    无法看出 cost 影响。修复后两条线应在 rebalance 日开始分叉。
    """
    close = _make_close(
        date(2026, 4, 1),
        {
            "A": [10.0, 10.0, 10.0, 10.0],
            "B": [20.0, 20.0, 20.0, 20.0],
            "000300": [3000.0, 3000.0, 3000.0, 3000.0],
        },
    )
    weights_by_date = {
        "2026-04-01": {"A": 1.0},
        "2026-04-02": {"B": 1.0},  # 100% 换手
    }
    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0003, slippage=0.0005),
    )
    nav_df = bt._replay(weights_by_date, close)

    # 横盘 + rebalance → nav_gross 不变 (1.0)，nav_net 因 cost 下降
    day2 = nav_df.iloc[1]
    assert abs(float(day2["nav_gross"]) - 1.0) < 1e-9, "横盘时 nav_gross 应保持 1.0"
    assert float(day2["nav_net"]) < 1.0, "rebalance 后 nav_net 应因 cost 下降"
    # gross / net 必须分叉
    assert abs(float(day2["nav_gross"]) - float(day2["nav_net"])) > 1e-6, (
        "nav_gross 不应等于 nav_net (否则曲线重合，cost 不可见)"
    )


def test_backtester_no_lookahead_signal_day_not_traded(tmp_path: Path) -> None:
    """前视偏差 canary: T 日信号绝不能用 T 日收盘价成交, 必须 T+1 建仓。

    构造: 信号日 (day0) 当天该票暴涨, 次日 (day1) 起横盘。若存在前视偏差
    (T 日信号用 T 日 close 成交), 组合会"吃到"day0 的暴涨; 正确的 T+1 成交
    则错过 day0 涨幅、只从 day1 建仓, nav 应保持 ~1.0。
    """
    close = _make_close(
        date(2026, 5, 6),
        {
            # day0 暴涨 (10→13, +30%), day1 起横盘在 13
            "000001": [10.0, 13.0, 13.0, 13.0],
            "000300": [3000.0, 3000.0, 3000.0, 3000.0],
        },
    )
    weights_by_date = {"2026-05-06": {"000001": 1.0}}  # day0 收盘后的信号

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, benchmark_symbol="000300"),
    )
    nav_df = bt._replay(weights_by_date, close)

    final_nav = float(nav_df.iloc[-1]["nav_net"])
    # T+1 正确: 从 day1 (price=13) 建仓, 之后横盘 → nav 全程 ~1.0, 完全错过 +30%
    assert abs(final_nav - 1.0) < 1e-6, (
        f"前视偏差! final nav={final_nav:.4f} 明显 >1.0 说明组合吃到了信号日当天的 "
        f"+30% 暴涨 —— T 日信号被用 T 日 close 成交了。正确 T+1 成交应错过该涨幅、nav≈1.0"
    )


def test_backtester_price_limit_up_blocks_buy(tmp_path: Path) -> None:
    """涨跌停 canary: 目标要买入但当日涨停的票, 无法建仓 (禁买)。

    day0 信号买入 000001; day1 (T+1 成交日) 000001 相对 day0 涨 +12% (>9.5% 涨停),
    应禁止买入 → 该票不进 shares → 组合空仓, nav 保持 1.0。
    """
    close = _make_close(
        date(2026, 6, 1),
        {
            # day0=10, day1 涨停 (10→11.2, +12%)
            "000001": [10.0, 11.2, 11.2, 11.2],
            "000300": [3000.0, 3000.0, 3000.0, 3000.0],
        },
    )
    weights_by_date = {"2026-06-01": {"000001": 1.0}}

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, benchmark_symbol="000300",
                           price_limit_pct=0.095),
    )
    nav_df = bt._replay(weights_by_date, close)
    # 涨停禁买 → 首个建仓日空仓, nav 应保持 1.0 (没吃到后续任何波动)
    final_nav = float(nav_df.iloc[-1]["nav_net"])
    assert abs(final_nav - 1.0) < 1e-6, (
        f"涨停禁买失效! nav={final_nav:.4f}, 应=1.0 (涨停无法建仓 → 空仓)"
    )
