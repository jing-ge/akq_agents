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


def test_summarize_annualization_uses_calendar_span_not_trading_days(tmp_path: Path) -> None:
    """年化收益必须按实际日历跨度, 不能假设 n 个交易日 = 满年 (252) 外推。

    历史 bug: ann = nav_last ** (252/n) - 1, 当回测不足一年时把短期收益
    外推成整年, 严重高估。例: 20 个交易日涨 10%, 用 252/20 会外推成 +230%+,
    而实际约 1 个月的年化应贴近"按日历跨度折算"的值。
    """
    # 构造约半年 (126 个交易日) 的净值, 组合从 1.0 涨到 1.10。
    # 用连续自然日做索引简化 (跨度 = n-1 天), 关键是验证年化按日历跨度而非 252/n 外推。
    n = 126
    navs = [1.0 + 0.10 * i / (n - 1) for i in range(n)]
    dates = [date(2026, 1, 5).toordinal() + i for i in range(n)]
    nav_df = pd.DataFrame({
        "as_of_date": [date.fromordinal(o).isoformat() for o in dates],
        "nav_net": navs,
        "daily_return_net": [0.0] + [navs[i] / navs[i - 1] - 1.0 for i in range(1, n)],
        "turnover": [0.0] * n,
        "cost": [0.0] * n,
        "benchmark_nav": [None] * n,
        "benchmark_return": [None] * n,
    })
    s = PortfolioBacktester._summarize(nav_df)
    ann = s["annualized_return_net"]
    # 跨度 = 125 自然日 ≈ 0.342 年, 涨 10% → 年化 ≈ 1.10^(1/0.342)-1 ≈ 32%。
    # 锁定: 按日历跨度年化应落在合理区间, 而非把 126 交易日硬当 252/n 外推。
    span_years = (date.fromordinal(dates[-1]) - date.fromordinal(dates[0])).days / 365.25
    expected = 1.10 ** (1.0 / span_years) - 1.0
    assert abs(ann - expected) < 0.02, (
        f"年化 {ann*100:.1f}% 应按日历跨度 ({span_years:.3f}年) 折算 ≈ {expected*100:.1f}%, "
        f"而非 252/n 外推"
    )
    # 短窗口反例: 旧 252/n 公式在 n 很小时会爆炸 (5 交易日涨 20% → +978792%)。
    # 新公式按日历跨度, 同样数据不会给出这种荒谬值。
    short = pd.DataFrame({
        "as_of_date": [date(2026, 6, 1 + i).isoformat() for i in range(5)],
        "nav_net": [1.0, 1.05, 1.10, 1.15, 1.20],
        "daily_return_net": [0.0, 0.05, 0.0476, 0.0455, 0.0435],
        "turnover": [0.0] * 5, "cost": [0.0] * 5,
        "benchmark_nav": [None] * 5, "benchmark_return": [None] * 5,
    })
    ann_short = PortfolioBacktester._summarize(short)["annualized_return_net"]
    assert ann_short is None, (
        f"5 日 (跨度4天) 的年化应为 None (跨度不足不可外推), 而非 {ann_short} "
        f"(旧 252/n bug 会给 ~9788x)"
    )


def test_summarize_excess_and_aligned_total_return_are_consistent(tmp_path: Path) -> None:
    """excess 与其对齐口径的组合收益必须内部一致, 不能一个用全表末日、一个用基准末日。

    历史 bug: total_return_net 用全表末日 nav, excess 用基准对齐日 nav,
    当基准数据比组合短 (末端 benchmark_nav 为 NULL) 时, summary 内部自相矛盾。
    """
    n = 5
    nav_df = pd.DataFrame({
        "as_of_date": [date(2026, 6, 1 + i).isoformat() for i in range(n)],
        "nav_net": [1.0, 1.05, 1.10, 1.15, 1.20],
        "daily_return_net": [0.0, 0.05, 0.0476, 0.0455, 0.0435],
        "turnover": [0.0] * n,
        "cost": [0.0] * n,
        # 基准只到第 3 天 (后 2 天 NULL), 模拟基准数据滞后
        "benchmark_nav": [1.0, 1.02, 1.04, None, None],
        "benchmark_return": [0.0, 0.02, 0.0196, None, None],
    })
    s = PortfolioBacktester._summarize(nav_df)
    # excess = 对齐到基准末日 (第3天, nav=1.10) 的组合收益 - 基准收益
    # = (1.10-1) - (1.04-1) = 0.10 - 0.04 = 0.06
    assert abs(s["excess_return"] - 0.06) < 1e-9, f"excess={s['excess_return']}"
    # 必须提供与 excess 同口径的组合收益 (对齐到基准末日), 保证内部可核对:
    # aligned_total - benchmark_total == excess
    assert "total_return_net_aligned" in s, "缺少与 excess 同口径的对齐组合收益, summary 无法自洽核对"
    assert abs(s["total_return_net_aligned"] - 0.10) < 1e-9, (
        f"对齐组合收益应为 0.10 (基准末日组合 nav=1.10), 实际 {s['total_return_net_aligned']}"
    )
    assert abs(
        s["total_return_net_aligned"] - s["benchmark_total_return"] - s["excess_return"]
    ) < 1e-9, "aligned_total - benchmark_total 必须等于 excess (口径自洽)"


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
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0, benchmark_symbol="000300"),
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
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0),
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
        cfg=BacktestConfig(commission=0.0003, slippage=0.0005, stamp_duty=0.0,
                           benchmark_symbol="000300"),
    )
    nav_df = bt._replay(weights_by_date, close)

    # 第二日 rebalance turnover 应为 1.0 (A↔B 100% 换仓)
    day2 = nav_df.iloc[1]
    assert abs(float(day2["turnover"]) - 1.0) < 1e-9
    # 双边 cost：2 × 1.0 × 0.0008 = 0.0016（如果还是单边 bug 会得到 0.0008）
    # 这里显式 stamp_duty=0 单独测双边佣金+滑点; 印花税另有专门 canary。
    assert abs(float(day2["cost"]) - 0.0016) < 1e-9, (
        f"R4 失败：rebalance 100% 换手的 cost 应为 0.0016（双边），实际 {day2['cost']:.6f}"
    )
    # 这里**不**断言 nav_net 的绝对值，因为首日建仓也有 cost；
    # 只断言 day2 单步 cost 含义 + day2 nav 较 day1 下降 ≈ 0.0016


def test_backtester_stamp_duty_charged_on_sell_side_only(tmp_path: Path) -> None:
    """印花税 canary: A股卖出单边征印花税 (0.05%), 买入不征。

    成本模型: turnover 单边换手率, cost = turnover × [2×(commission+slippage) + stamp_duty]
    (买入边 commission+slippage, 卖出边 commission+slippage+stamp_duty)。
    场景: 100% 换手 (A→B), turnover=1.0, commission=slippage=0, stamp_duty=0.0005
    → cost 应 = 1.0 × (0 + 0.0005) = 0.0005 (只有卖出印花税)。
    """
    close = _make_close(
        date(2026, 8, 3),
        {"A": [10.0, 10.0, 10.0], "B": [20.0, 20.0, 20.0], "000300": [3000.0] * 3},
    )
    weights_by_date = {"2026-08-03": {"A": 1.0}, "2026-08-04": {"B": 1.0}}
    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0005,
                           benchmark_symbol="000300"),
    )
    nav_df = bt._replay(weights_by_date, close)
    day2 = nav_df.iloc[1]
    assert abs(float(day2["turnover"]) - 1.0) < 1e-9
    # 只有卖出印花税: 1.0 × 0.0005 = 0.0005
    assert abs(float(day2["cost"]) - 0.0005) < 1e-9, (
        f"印花税应只在卖出边征收, 100%换手 cost 应=0.0005, 实际 {day2['cost']:.6f}"
    )


def test_backtester_stamp_duty_defaults_nonzero(tmp_path: Path) -> None:
    """印花税默认应为正 (真实 A股成本), 不能默认 0 让回测系统性高估收益。"""
    assert BacktestConfig().stamp_duty > 0, "stamp_duty 默认应 > 0 (A股卖出印花税真实存在)"


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
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0, benchmark_symbol="000300"),
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
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0, benchmark_symbol="000300",
                           price_limit_pct=0.095),
    )
    nav_df = bt._replay(weights_by_date, close)
    # 涨停禁买 → 首个建仓日空仓, nav 应保持 1.0 (没吃到后续任何波动)
    final_nav = float(nav_df.iloc[-1]["nav_net"])
    assert abs(final_nav - 1.0) < 1e-6, (
        f"涨停禁买失效! nav={final_nav:.4f}, 应=1.0 (涨停无法建仓 → 空仓)"
    )


def test_backtester_unfilled_weight_does_not_vanish(tmp_path: Path) -> None:
    """守恒 canary: 部分票无法成交时, 未成交权重必须留存为现金, 不能凭空蒸发。

    历史 bug: rebalance 后 nav_net 记全额, 但重建 shares 只覆盖能成交的票 →
    Σ(shares×px) < nav_net (脱节)。下一日 mv/prev_mv 把丢失的权重变成虚假亏损,
    误差每次调仓累积, 200 天把净值从合理水平啃到 0.18。

    构造: 组合 A(50%) + B(50%)。B 在建仓日 (day1) 涨停禁买 → 只有 A 成交。
    正确行为: A 拿到 50% 资金, 剩余 50% 留现金; 之后 A、B 都横盘 →
    nav 应保持 1.0 (现金不波动, A 也不波动)。
    bug 行为: 50% 权重蒸发, nav 掉到 ~0.5 或下一日出现巨大假跳空。
    """
    close = _make_close(
        date(2026, 7, 1),
        {
            "A": [10.0, 10.0, 10.0, 10.0],       # 全程横盘, 可正常成交
            # B: day0=10, day1 涨停 (10→11.2, +12% > 9.5%) 禁买, 之后横盘
            "B": [10.0, 11.2, 11.2, 11.2],
            "000300": [3000.0, 3000.0, 3000.0, 3000.0],
        },
    )
    weights_by_date = {"2026-07-01": {"A": 0.5, "B": 0.5}}

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0, benchmark_symbol="000300",
                           price_limit_pct=0.095),
    )
    nav_df = bt._replay(weights_by_date, close)

    # B 禁买 → 50% 留现金, A 与现金都不波动 → nav 全程 1.0
    for _, row in nav_df.iterrows():
        assert abs(float(row["nav_net"]) - 1.0) < 1e-6, (
            f"未成交权重蒸发! {row['as_of_date']} nav={row['nav_net']:.4f}, 应=1.0。"
            f"B 涨停禁买的 50% 权重应留现金, 而非凭空亏损。"
        )
    # 且任何一日都不应出现虚假跳空
    max_daily = float(nav_df["daily_return_net"].abs().max())
    assert max_daily < 1e-6, (
        f"出现虚假日跳空 {max_daily*100:.2f}% —— nav_net 与实际持仓市值脱节的典型症状"
    )


def test_backtester_price_limit_is_board_aware(tmp_path: Path) -> None:
    """涨跌停阈值须按板块区分: 创业板/科创板 ±20%, 主板 ±10%。

    历史精度问题: 用统一 9.5% 阈值, 创业板涨 15%(未涨停,可交易)被误判涨停禁买,
    组合少吃这部分涨幅 → 收益偏低估。持仓池创业板占比高达 32%, 影响不可忽略。

    场景: 主板 000001 涨 12%(>10% 主板涨停 → 禁买), 创业板 300001 涨 15%
    (<20% 创业板涨停 → 可正常买入)。
    """
    close = _make_close(
        date(2026, 9, 1),
        {
            # 主板: day1 涨 12% → 超过主板 ±10% → 涨停禁买
            "000001": [10.0, 11.2, 11.2, 11.2],
            # 创业板: day1 涨 15% → 未超创业板 ±20% → 可买入
            "300001": [10.0, 11.5, 11.5, 11.5],
            "000300": [3000.0] * 4,
        },
    )
    weights_by_date = {"2026-09-01": {"000001": 0.5, "300001": 0.5}}
    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0,
                           benchmark_symbol="000300", price_limit_pct=0.095),
    )
    nav_df = bt._replay(weights_by_date, close)
    # 建仓日(day1): 000001 涨停禁买 → 50% 现金; 300001 可买 → 建仓当日 nav=1.0(刚建仓)
    # day2/day3: 300001 横盘在 11.5, 000001 现金不动 → nav 保持 1.0
    # 关键: 若 300001 被误判涨停禁买, 则它也进现金, 与主板行为无差异, 测不出区别。
    # 故用"建仓后 300001 继续涨"来区分: 改造场景见下。
    # 这里断言: 建仓日 300001 成功建仓(占一半)。用 replay 的 shares 侧信息不易取,
    # 改为验证: 若 300001 建仓成功, 后续它的价格变动应反映到 nav。
    # 重构价格: 让 300001 在 day2 再涨, 若建仓成功 nav 会涨。
    close2 = _make_close(
        date(2026, 9, 1),
        {
            "000001": [10.0, 11.2, 11.2, 11.2],       # 主板涨停禁买, 全程现金
            "300001": [10.0, 11.5, 12.65, 12.65],     # 创业板 day1 建仓, day2 再涨10%
            "000300": [3000.0] * 4,
        },
    )
    bt2 = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db2",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, stamp_duty=0.0,
                           benchmark_symbol="000300", price_limit_pct=0.095),
    )
    nav2 = bt2._replay(weights_by_date, close2)
    final = float(nav2.iloc[-1]["nav_net"])
    # 300001 建仓成功(占50%), day2 涨10% → 组合 nav ≈ 1.0 + 0.5×10% = 1.05
    # 若 300001 被误判涨停禁买(旧 9.5% 阈值) → 它进现金, nav 保持 1.0
    assert final > 1.03, (
        f"创业板 300001 涨15%(未到±20%涨停)应可建仓, day2 再涨10% 应推 nav 到 ~1.05, "
        f"实际 {final:.4f}。<1.03 说明被 9.5% 阈值误判涨停禁买了(板块阈值未生效)"
    )
