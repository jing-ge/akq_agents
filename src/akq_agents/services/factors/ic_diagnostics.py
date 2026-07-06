"""P2-4 Step 1: 因子 IC 诊断 (离线试算不同 IC 计算方式对当前入选因子的影响).

目标: 给用户量化地看到"如果开启 winsor / industry_neutral, IC 会从 X 提到 Y",
      作为是否值得做 Step 2 (真改评估器) 的决策依据.

**只读, 不改任何持久化状态**. 不写 factor_metrics, 不影响生产流水.

跑一次: 对当前 registry.list_all() 的 active 因子, 各拉 90 天 factor_history + close,
       跑 4 个 IC 版本 (baseline / winsor / industry_neutral / combined),
       返回入选因子的 IC 均值对比.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _compute_forward_returns(close: pd.DataFrame) -> pd.DataFrame:
    """T+1 收益 = close.pct_change().shift(-1)"""
    return close.pct_change(fill_method=None).shift(-1)


def _winsorize_row(row: pd.Series, low: float = 0.05, high: float = 0.95) -> pd.Series:
    """把 series 的极端值 clip 到 [low_quantile, high_quantile] 区间."""
    if row.isna().all():
        return row
    lo = row.quantile(low)
    hi = row.quantile(high)
    return row.clip(lower=lo, upper=hi)


def _industry_demean_row(row: pd.Series, industry_map: dict[str, str]) -> pd.Series:
    """把 series 中每个 symbol 的值减去该行业均值 (行业中性化)."""
    if not industry_map:
        return row
    # 建 symbol -> industry
    ind = pd.Series({s: industry_map.get(s) for s in row.index}, name="industry")
    df = pd.DataFrame({"v": row, "ind": ind}).dropna(subset=["ind"])
    if df.empty:
        return row
    means = df.groupby("ind")["v"].transform("mean")
    demeaned = df["v"] - means
    return pd.Series(demeaned, index=df.index).reindex(row.index)


def _rolling_spearman_ic(
    factor_history: pd.DataFrame,
    forward_returns: pd.DataFrame,
    *,
    winsor: bool = False,
    industry_map: dict[str, str] | None = None,
) -> float | None:
    """算最近 30 天的平均 spearman IC. 返回单值 (期均值).

    winsor=True → 因子值 5%/95% winsorize
    industry_map 非空 → 因子值行业内 demean
    """
    aligned_idx = factor_history.index.intersection(forward_returns.index)
    if len(aligned_idx) < 5:
        return None
    lookback = min(30, len(aligned_idx))
    f = factor_history.loc[aligned_idx].tail(lookback)
    r = forward_returns.loc[aligned_idx].tail(lookback)
    ics: list[float] = []
    for d in f.index:
        f_row = pd.Series(f.loc[d]).dropna()
        r_row = pd.Series(r.loc[d]).dropna()
        common = f_row.index.intersection(r_row.index)
        if len(common) < 3:
            continue
        fr = f_row.loc[common]
        rr = r_row.loc[common]
        if winsor:
            fr = _winsorize_row(fr)
        if industry_map:
            fr = _industry_demean_row(fr, industry_map).dropna()
            common2 = fr.index.intersection(rr.index)
            if len(common2) < 3:
                continue
            fr = fr.loc[common2]
            rr = rr.loc[common2]
        ic = fr.rank().corr(rr.rank())
        if pd.notna(ic):
            ics.append(float(ic))
    if not ics:
        return None
    return sum(ics) / len(ics)


def diagnose_selected_factors(
    *,
    repo: Any,
    registry: Any,
    industry_map: dict[str, str],
    selected_names: list[str] | None = None,
    lookback_days: int = 30,
    max_factors: int = 8,
) -> dict[str, Any]:
    """跑 4 种 IC 计算方式对比. 返回每因子每模式的 IC 与均值统计.

    selected_names: 只对这些因子跑. None 则对 registry 里全部 active 因子跑.
    lookback_days: 拉多少天日线做 factor_history + IC 计算. 默认 30 天足够
                    对比 baseline vs winsor/industry_neutral 收益, 不用 90 天.
    max_factors: 最多跑几个因子 (avoid 26 因子 × 30 天 × 5521 股票的爆炸计算).
    """
    from datetime import date, timedelta
    from akq_agents.services.factors.history_backfill import (
        _default_compute_factor_history,
    )

    # 找最近一个 ohlcv 分区 (今天 today_batch 未跑时, universe 拿不到)
    end = None
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    if ohlcv_dir is not None and ohlcv_dir.exists():
        candidates = []
        for p in ohlcv_dir.iterdir():
            n = p.name
            if n.startswith("date=") and len(n) == 15:
                try:
                    candidates.append(date.fromisoformat(n[5:]))
                except ValueError:
                    continue
        if candidates:
            end = max(candidates)
    if end is None:
        end = date.today()
    start = end - timedelta(days=int(lookback_days * 1.6) + 10)

    try:
        universe = repo.get_universe(end)
        symbols = list(universe.symbols) if universe is not None else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("ic_diagnostics: get_universe(%s) failed: %s", end, exc)
        symbols = []
    if not symbols:
        return {"error": f"no_universe_at_{end.isoformat()}", "factors": []}
    try:
        ohlcv = repo.get_ohlcv_loose(symbols, start, end)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ohlcv_fetch_failed: {exc}", "factors": []}
    if ohlcv is None or ohlcv.empty:
        return {"error": "empty_ohlcv", "factors": []}

    # 拼 close DataFrame (date x symbol). **不 to_datetime**: ohlcv['date'] 是 str,
    # _default_compute_factor_history 里 `sub['date'] <= d_date` 要求 sub 与 d_date 同类型.
    # backfill 侧的 close 也直接 str-pivot, all_dates 传 str 给它, 保持契约一致.
    close = ohlcv.pivot_table(
        index="date", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    forward_returns = _compute_forward_returns(close)
    logger.info(
        "ic_diagnostics: universe=%d, ohlcv rows=%d, close shape=%s",
        len(symbols), len(ohlcv), close.shape,
    )

    # 挑要诊断的因子
    all_factors = registry.list_all()
    if selected_names is not None:
        factors_to_run = [f for f in all_factors if f.name in set(selected_names)]
    else:
        factors_to_run = all_factors
    # 硬上限, 防止 26 因子 × 100 天 × 5521 股票的爆炸性组合
    if len(factors_to_run) > max_factors:
        factors_to_run = factors_to_run[:max_factors]
        logger.info("ic_diagnostics: capped factors_to_run at max_factors=%d", max_factors)

    results: list[dict[str, Any]] = []
    n_hist_ok = 0
    n_hist_empty = 0
    n_hist_error = 0
    for f in factors_to_run:
        try:
            hist = _default_compute_factor_history(f, ohlcv, close.index)
        except Exception as exc:  # noqa: BLE001
            n_hist_error += 1
            if n_hist_error <= 3:
                logger.warning("ic_diagnostics: hist compute failed for %s: %s", f.name, exc)
            continue
        if hist is None or hist.empty:
            n_hist_empty += 1
            if n_hist_empty <= 3:
                logger.warning(
                    "ic_diagnostics: hist empty for %s (lookback_days=%s, ohlcv_dates_range=[%s..%s])",
                    f.name, getattr(f, "lookback_days", "?"),
                    ohlcv["date"].min(), ohlcv["date"].max(),
                )
            continue
        n_hist_ok += 1
        row: dict[str, Any] = {"name": f.name, "direction": f.direction}
        row["ic_baseline"] = _rolling_spearman_ic(hist, forward_returns)
        row["ic_winsor"] = _rolling_spearman_ic(hist, forward_returns, winsor=True)
        row["ic_industry_neutral"] = _rolling_spearman_ic(
            hist, forward_returns, industry_map=industry_map,
        )
        row["ic_combined"] = _rolling_spearman_ic(
            hist, forward_returns, winsor=True, industry_map=industry_map,
        )
        results.append(row)

    logger.info(
        "ic_diagnostics: for-loop done: n_factors_input=%d, hist_ok=%d, hist_empty=%d, hist_error=%d",
        len(factors_to_run), n_hist_ok, n_hist_empty, n_hist_error,
    )

    # 汇总: 4 种 mode 各自的 |IC| 均值 (只对当前入选因子)
    def _mean_abs(mode: str) -> float | None:
        vals = [abs(r[mode]) for r in results if r.get(mode) is not None]
        return (sum(vals) / len(vals)) if vals else None

    summary = {
        "n_factors": len(results),
        "mean_abs_ic_baseline": _mean_abs("ic_baseline"),
        "mean_abs_ic_winsor": _mean_abs("ic_winsor"),
        "mean_abs_ic_industry_neutral": _mean_abs("ic_industry_neutral"),
        "mean_abs_ic_combined": _mean_abs("ic_combined"),
    }
    # 相对基线的提升幅度 (%)
    b = summary["mean_abs_ic_baseline"]
    if b and b > 0:
        for k in ("mean_abs_ic_winsor", "mean_abs_ic_industry_neutral", "mean_abs_ic_combined"):
            v = summary.get(k)
            summary[k + "_lift_pct"] = ((v / b - 1) * 100) if v else None
    return {"factors": results, "summary": summary}
