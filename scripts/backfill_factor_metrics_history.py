"""补 factor_metrics 历史数据 (M19)。

用户场景: LLM brainstorm 提议的 shadow 因子, 当天起才进 factor_proposals,
factor_metrics 表里只有今天 1 条。用户想看完整 IC 曲线做人工决策, 但 OOS
评估仍按真规则跑 (shadow_started_at 不动)。

本脚本: 对 (--filter) 匹配的因子, 循环过去 N 个交易日, 用截止当日的 OHLCV
算因子值 + 当日 IC, 调 evaluator.evaluate 写 factor_metrics。

复用 DiscoveryEngine._prepare_data + _compute_factor_history (它们已经实现
"截止 as_of_date 的数据计算因子"), 避免重写逻辑。

用法:
    PYTHONPATH=src python scripts/backfill_factor_metrics_history.py \\
        --filter 'llm_*' --days 90

注意: 不改 factor_proposals.shadow_started_at 也不改 oos_observations,
OOS promote 逻辑仍按真时间线走。
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# 让脚本能 import akq_agents
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from akq_agents.bootstrap import build_workflow  # noqa: E402
from akq_agents.services.factors.discovery import make_factor  # noqa: E402
from akq_agents.services.factors.proposal_store import recipe_from_json  # noqa: E402


def _collect_factors_inline(factor_registry, proposal_store, repo, filter_glob: str):
    """收集要补的因子: registry 全集 + proposal_store 全集, 应用 filter_glob 过滤."""
    out: list[tuple[str, object]] = []
    seen: set[str] = set()
    if factor_registry is not None:
        for f in factor_registry.list_all():
            if not fnmatch.fnmatch(f.name, filter_glob):
                continue
            seen.add(f.name)
            out.append((f.name, f))
    if proposal_store is not None and repo is not None:
        from akq_agents.services.data.repository import open_meta_db
        db_path = repo._base_dir / "meta.db"
        with open_meta_db(db_path) as conn:
            rows = conn.execute(
                "SELECT factor_name, recipe_json, status, reason "
                "FROM factor_proposals "
                "WHERE status IN ('accepted','shadow','rejected','demoted')"
            ).fetchall()
        for name, recipe_json, status, reason in rows:
            if name in seen:
                continue
            if not fnmatch.fnmatch(name, filter_glob):
                continue
            # 跳过 compute_error 类 (recipe 跑不出值的死因子)
            if status == "rejected" and reason and reason.startswith("compute_error"):
                continue
            try:
                recipe = recipe_from_json(recipe_json)
                factor = make_factor(recipe)
                factor.name = name  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"  [skip] make_factor({name}) failed: {exc}")
                continue
            seen.add(name)
            out.append((name, factor))
    return out


def main(filter_glob: str, days: int, step: int) -> None:
    import pandas as pd

    print(f"[*] 启动 backfill: filter={filter_glob!r} days={days} step={step}")
    t_init = time.monotonic()

    wf, _ = build_workflow()
    svc_registry = wf.services.get("factor_registry")
    svc_proposal = wf.services.get("factor_proposal_store")
    svc_repo = wf.services.get("data_repository")
    targets = _collect_factors_inline(svc_registry, svc_proposal, svc_repo, filter_glob)
    if not targets:
        print(f"[!] 没找到匹配 {filter_glob!r} 的因子, 退出")
        return
    print(f"[*] 匹配 {len(targets)} 个因子, init {time.monotonic()-t_init:.1f}s")

    repo = wf.services["data_repository"]
    evaluator = wf.services["factor_evaluator"]
    engine = wf.services["discovery_engine"]
    db_path = repo._base_dir / "meta.db"

    today = date.today()
    # 拉一段足够长的 OHLCV (max_lookback 留余量 + days + evaluator window)
    max_lb = max((getattr(f, "lookback_days", 60) for _, f in targets), default=60)
    window = getattr(evaluator, "_window", 60)
    pull_start = today - timedelta(days=(max_lb + window + days) * 2)
    print(f"[*] 拉 OHLCV {pull_start} -> {today}")
    t_pull = time.monotonic()
    try:
        full_universe = repo.get_universe(today)
    except Exception:
        full_universe = repo.get_universe(today - timedelta(days=1))
    ohlcv = repo.get_ohlcv_loose(full_universe.symbols, pull_start, today)
    if ohlcv.empty:
        print(f"[!] ohlcv empty, 退出")
        return
    print(f"[*] ohlcv shape: {ohlcv.shape}, 拉数据 {time.monotonic()-t_pull:.1f}s")

    # 限制 universe (与 discovery 一致 top 300 by 流动性)
    from akq_agents.services.portfolio.combined_universe import build_portfolio_universe
    sub_symbols = build_portfolio_universe(
        full_universe_symbols=full_universe.symbols, ohlcv=ohlcv, top_n=300, window=20
    )
    sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(sub_symbols))]
    close = sub_ohlcv.pivot_table(
        index="date", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    forward_returns = close.pct_change(fill_method=None).shift(-1)

    # 找 N 个 as_of_date 节点 (从最近往前数, 每 step 天一个)
    all_dates = list(close.index)
    if len(all_dates) < window + days:
        print(f"[!] 历史数据不足 ({len(all_dates)} 天 < {window+days}), 退出")
        return
    # 取最近 `days` 个交易日, 每 step 天采一次
    candidate_dates = all_dates[-days:][::step]
    print(f"[*] 选 {len(candidate_dates)} 个 as_of_date 节点 (step={step})")

    total_written = 0
    t_eval = time.monotonic()
    for fi, (name, factor) in enumerate(targets):
        try:
            # 一次性算完整 factor_history (覆盖全段)
            factor_history = engine._compute_factor_history(factor, sub_ohlcv, close.index)
        except Exception as exc:
            print(f"  [{fi+1}/{len(targets)}] {name}: factor_history failed: {exc}")
            continue
        if factor_history is None or factor_history.empty:
            print(f"  [{fi+1}/{len(targets)}] {name}: empty factor_history")
            continue

        # 对每个 as_of_date, 调 evaluator.evaluate (它内部用 .tail(window) 取滚动窗口)
        n_factor = 0
        for as_of in candidate_dates:
            as_of_d = as_of.date() if hasattr(as_of, "date") else as_of
            # 只用截止 as_of 的数据
            fh_sub = factor_history.loc[:as_of]
            fr_sub = forward_returns.loc[:as_of]
            common_idx = fh_sub.index.intersection(fr_sub.index)
            if len(common_idx) < window:
                continue
            try:
                evaluator.evaluate(
                    factor=factor,
                    factor_history=fh_sub.loc[common_idx],
                    forward_returns=fr_sub.loc[common_idx],
                    as_of_date=as_of_d,
                )
                n_factor += 1
                total_written += 1
            except Exception as exc:
                print(f"     evaluate({name}, {as_of_d}) failed: {exc}")
        # 补历史不按时间顺序写入, evaluator 内部 _read_recent_history 看到的是
        # "未来"日期的 row, status 判定 (low_ir_persistent / low_ir_observed_x/5)
        # 没有意义且会误标 inactive 影响 registry.list_active。统一把这段 backfill
        # 写入的 status 改回 active, reason='backfill', 不污染线上判定。
        _mark_backfill(db_path, name, candidate_dates)
        print(f"  [{fi+1}/{len(targets)}] {name}: wrote {n_factor} rows")

    print(f"[OK] 共写入 {total_written} 行, 评估 {time.monotonic()-t_eval:.1f}s, "
          f"总耗时 {time.monotonic()-t_init:.1f}s")


def _mark_backfill(db_path: Path, factor_name: str, as_of_dates) -> None:
    """把这批补历史写的行 status 改回 active, reason='backfill'.

    evaluator.evaluate 内部 _read_recent_history 在补历史场景下读到的是"未来"日期的
    row, 会错算"低 IR 持续"标 inactive。补历史的 status 字段本来就没意义 (因为离线
    回填没有真实时间顺序), 统一标 active 避免污染 registry.list_active。
    """
    from akq_agents.services.data.repository import open_meta_db
    dates_iso = [(d.date() if hasattr(d, "date") else d).isoformat() for d in as_of_dates]
    if not dates_iso:
        return
    placeholders = ",".join(["?"] * len(dates_iso))
    with open_meta_db(db_path) as conn:
        conn.execute(
            f"UPDATE factor_metrics SET status='active', reason='backfill' "
            f"WHERE factor_name=? AND as_of_date IN ({placeholders})",
            (factor_name, *dates_iso),
        )
        conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="补 factor_metrics 历史 IC 数据")
    parser.add_argument("--filter", default="llm_*",
                        help="因子名 glob (默认 llm_*; 也可写 'auto_*' / '*')")
    parser.add_argument("--days", type=int, default=90,
                        help="向前回填 N 个交易日 (默认 90)")
    parser.add_argument("--step", type=int, default=1,
                        help="每 step 天采一个 as_of_date (默认 1=每天)")
    args = parser.parse_args()
    main(args.filter, args.days, args.step)
