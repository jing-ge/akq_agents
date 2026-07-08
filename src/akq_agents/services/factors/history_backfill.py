"""新因子入库时算 90 天历史 IC, 写 factor_metrics + 同步 factor_proposals.

设计目标 (M19, 用户需求"新因子入库的时候把历史的 ICIR 都计算一遍"):
- LLMFactorBrainstormer 入 status='llm_suggested' 时调用 → 用户审核界面看得到 90 天 IC 趋势
- DiscoveryEngine.run_batch 候选评估时调用 → 不只是当日 IC, 直接看完整曲线判断
- web /factors/llm-suggestions/{name}/accept 也调同一函数

为什么不复用 evaluator.evaluate 直接调:
1. evaluator.evaluate 只算 1 个 as_of_date, 调 90 次的话 factor_history 重复算 90 倍
2. 离线 backfill 时 evaluator._read_recent_history 看到的是"未来"日期的 row,
   会错标 status='inactive' 污染 registry.list_active. 这里统一标
   reason='backfill', status='active' 避免

性能 (实测): 单因子约 2.5s (拉数据 + factor_history + 90 次 evaluate.upsert).
    20 个因子约 50s, 1 次拉数据复用 → 实际 30s。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from akq_agents.services.data.repository import open_meta_db
from akq_agents.services.factors.base import Factor, compute_forward_returns

logger = logging.getLogger(__name__)


@dataclass
class HistoryBackfillContext:
    """共享上下文: ohlcv / close pivot / forward_returns / as_of_dates.

    一轮 brainstorm 产 20 个因子时 build 一次, 复用给所有因子, 避免 20 次重拉数据。
    """
    ohlcv: pd.DataFrame  # long-format
    close: pd.DataFrame  # index=date, columns=symbol
    forward_returns: pd.DataFrame
    candidate_dates: list  # 要写入 factor_metrics 的 as_of_date 列表 (DESC 最新在前)
    window: int
    as_of_today: date

    @classmethod
    def build(
        cls,
        *,
        repo: Any,
        evaluator: Any,
        as_of_date: date | None = None,
        days: int = 90,
        step: int = 1,
        top_n_universe: int = 300,
    ) -> HistoryBackfillContext | None:
        """从 repo 拉数据构造上下文; 数据不足 / universe 拿不到时返 None."""
        as_of = as_of_date or date.today()
        window = getattr(evaluator, "_window", 60)

        # M19 review: 周末 / 节假日 / 盘中触发时 today 没数据, 用 calendar 找最近交易日
        # (旧逻辑只回退 1 天到 today-1, 跨周末必死). 没 calendar 就硬上 today。
        cal = getattr(repo, "_calendar", None)
        if cal is not None:
            try:
                # 探活今日数据是否就绪
                repo.get_universe(as_of)
            except Exception:  # noqa: BLE001
                try:
                    as_of = cal.previous_trading_day(as_of)
                except Exception:  # noqa: BLE001
                    pass
            try:
                if not cal.is_trading_day(as_of):
                    as_of = cal.previous_trading_day(as_of)
            except Exception:  # noqa: BLE001
                pass

        try:
            universe = repo.get_universe(as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("history_backfill: get_universe(%s) failed: %s; fallback prev trading day",
                           as_of, exc)
            try:
                # 用 calendar 再退一步 (上面 fallback 失败时兜底)
                if cal is not None:
                    as_of = cal.previous_trading_day(as_of)
                else:
                    as_of = as_of - timedelta(days=1)
                universe = repo.get_universe(as_of)
            except Exception as exc2:  # noqa: BLE001
                logger.warning("history_backfill: universe fallback also failed: %s", exc2)
                return None

        # 拉一段足够长的 OHLCV (max_lookback 留余量 + days + window)
        max_lb = 180  # discovery._prepare_data 同款上限
        pull_start = as_of - timedelta(days=(max_lb + window + days) * 2)
        ohlcv = repo.get_ohlcv_loose(universe.symbols, pull_start, as_of)
        if ohlcv.empty:
            # ohlcv 空也尝试退一交易日
            try:
                prev_d = cal.previous_trading_day(as_of) if cal is not None else as_of - timedelta(days=1)
                ohlcv = repo.get_ohlcv_loose(universe.symbols, pull_start, prev_d)
                if not ohlcv.empty:
                    as_of = prev_d
            except Exception:  # noqa: BLE001
                pass
        if ohlcv.empty:
            logger.warning("history_backfill: ohlcv empty for as_of=%s", as_of)
            return None

        # 限制 universe (与 discovery 一致 top top_n_universe by 流动性)
        from akq_agents.services.portfolio.combined_universe import build_portfolio_universe
        sub_symbols = build_portfolio_universe(
            full_universe_symbols=universe.symbols, ohlcv=ohlcv, top_n=top_n_universe, window=20
        )
        sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(sub_symbols))]
        close = sub_ohlcv.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        forward_returns = compute_forward_returns(close)

        return cls._from_close(
            sub_ohlcv, close, forward_returns,
            window=window, days=days, step=step, as_of=as_of,
        )

    @classmethod
    def from_existing(
        cls,
        *,
        ohlcv: pd.DataFrame,
        close: pd.DataFrame,
        forward_returns: pd.DataFrame,
        window: int,
        days: int = 90,
        step: int = 1,
        as_of_date: date | None = None,
    ) -> HistoryBackfillContext | None:
        """复用调用方已经算好的 close + forward_returns (如 DiscoveryEngine.run_batch)。

        避免在 brainstorm/discovery 流程内再拉一次数据 → 一轮 brainstorm 20 因子额外
        ~50s 中 ~0.5s 是数据 IO, 主要还是 factor_history 计算。
        """
        return cls._from_close(
            ohlcv, close, forward_returns,
            window=window, days=days, step=step,
            as_of=as_of_date or date.today(),
        )

    @classmethod
    def _from_close(
        cls,
        ohlcv: pd.DataFrame,
        close: pd.DataFrame,
        forward_returns: pd.DataFrame,
        *,
        window: int,
        days: int,
        step: int,
        as_of: date,
    ) -> HistoryBackfillContext | None:
        all_dates = list(close.index)
        if len(all_dates) < window + days:
            logger.warning("history_backfill: insufficient dates (%d < %d+%d)",
                           len(all_dates), window, days)
            return None
        # 最近 days 个交易日, 每 step 天采一次, DESC 排 (最新在前)
        candidate_dates = all_dates[-days:][::step][::-1]
        return cls(
            ohlcv=ohlcv,
            close=close,
            forward_returns=forward_returns,
            candidate_dates=candidate_dates,
            window=window,
            as_of_today=as_of,
        )


def backfill_one(
    factor: Factor,
    ctx: HistoryBackfillContext,
    *,
    evaluator: Any,
    proposal_store: Any | None = None,
    compute_factor_history: Any | None = None,
    mode: str = "fast",
) -> dict[str, Any]:
    """对单个 factor 跑 90 天 IC, 写 factor_metrics + 同步到 factor_proposals.

    Args:
        factor: 已构造好的 Factor 实例 (name 必须正确, evaluator.evaluate 用它做 key)
        ctx: HistoryBackfillContext (一轮 brainstorm 多因子时复用)
        evaluator: FactorEvaluator
        proposal_store: 可选, 给则同步最新一期 ic/ir/t_stat 到 factor_proposals
        compute_factor_history: 可选 callable (factor, ohlcv, all_dates) -> DataFrame.
            不给就直接调 DiscoveryEngine 那个静态方法。允许调用方传自己的实现避免循环 import。
        mode: 'fast' (默认, 跳过 db 已有 (factor_name, as_of_date, window_days) 的日期,
            只算缺失部分) / 'full' (重算所有 90 天, 覆盖 db 已有行).

            场景:
            - fast: 新因子入库 / 每日 cron / 用户日常查看 — 一般 db 已有 89/90 行只缺今天一行,
              单因子 < 0.5s
            - full: 数据更新或修复 / 怀疑历史 IC 算错了 — 用户主动按按钮重算

    Returns:
        {
          "ok": bool, "n_metrics_written": int, "n_skipped": int,
          "latest_ic_mean": float | None, "latest_ir": float | None,
          "latest_t_stat": float | None, "reason": str | None
        }
    """
    if compute_factor_history is None:
        # 默认实现: 复制 DiscoveryEngine._compute_factor_history 逻辑 (避免循环 import)
        compute_factor_history = _default_compute_factor_history

    # fast 模式: 先查 db 已有的 (factor_name, as_of_date) 集合, 跳过已存在的
    existing_dates: set[str] = set()
    if mode == "fast":
        existing_dates = _read_existing_metric_dates(evaluator, factor.name, factor.factor_version)

    # 所有候选转 iso 字符串集合, 判定哪些需要算
    candidate_iso = [(d, (d.date() if hasattr(d, "date") else d).isoformat())
                     for d in ctx.candidate_dates]
    todo = [(d, iso) for d, iso in candidate_iso if iso not in existing_dates]
    n_skipped = len(candidate_iso) - len(todo)

    if not todo:
        # 全部已存在, 只需要同步 factor_proposals (拿最新一期)
        latest = _read_latest_metric_from_db(evaluator, factor.name, factor.factor_version)
        if proposal_store is not None and latest is not None:
            _sync_proposal(proposal_store, factor.name, latest)
        return {
            "ok": True,
            "n_metrics_written": 0,
            "n_skipped": n_skipped,
            "n_failed": 0,
            "failed_dates": [],
            "latest_ic_mean": getattr(latest, "ic_mean", None) if latest else None,
            "latest_ir": getattr(latest, "ir", None) if latest else None,
            "latest_t_stat": getattr(latest, "t_stat", None) if latest else None,
            "reason": "all_existing_fast_mode_skip",
        }

    try:
        factor_history = compute_factor_history(factor, ctx.ohlcv, ctx.close.index)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history_backfill: factor_history(%s) failed: %s", factor.name, exc)
        return {"ok": False, "reason": f"factor_history_failed: {exc}",
                "n_metrics_written": 0, "n_skipped": n_skipped,
                "n_failed": 0, "failed_dates": [],
                "latest_ic_mean": None, "latest_ir": None, "latest_t_stat": None}
    if factor_history is None or factor_history.empty:
        return {"ok": False, "reason": "factor_history_empty",
                "n_metrics_written": 0, "n_skipped": n_skipped,
                "n_failed": 0, "failed_dates": [],
                "latest_ic_mean": None, "latest_ir": None, "latest_t_stat": None}

    # M22: 收集所有 metric, 一次事务批量写。原来是 90 次单事务 = 90 次 commit,
    # 现在 = 1 次 commit. SQLite WAL 写锁从 90 次降到 1 次, 让 web 端读不再被写锁阻塞.
    # 方案 2: rolling IC 增量化 — evaluate_batch_fast 一次算全历史 IC, 逐 as_of 取 tail,
    # 与旧逐个 evaluate 数值严格等价 (tests/portfolio/test_rolling_ic_incremental_equiv.py)。
    n_written = 0
    latest_metric = None
    failed_dates: list[str] = []
    # 只把 common_idx >= window 的 as_of 交给 evaluate_batch_fast (与旧路径 continue 跳过
    # insufficient 的行为一致: 这些日期不写 metric、不计 n_written)。
    eligible: list = []  # [(as_of_d, as_of_orig)]
    for as_of, _iso in todo:  # candidate_dates DESC, 最新在前
        as_of_d = as_of.date() if hasattr(as_of, "date") else as_of
        fh_sub = factor_history.loc[:as_of]
        fr_sub = ctx.forward_returns.loc[:as_of]
        common_idx = fh_sub.index.intersection(fr_sub.index)
        if len(common_idx) < ctx.window:
            continue
        eligible.append(as_of_d)

    if eligible:
        try:
            # 传原始 factor_history / forward_returns 全量; evaluate_batch_fast 内部按
            # as_of 各自 .loc[:as_of] 对齐 (等价旧路径 fh_sub.loc[common_idx])。
            metrics = evaluator.evaluate_batch_fast(
                factor=factor,
                factor_history=factor_history,
                forward_returns=ctx.forward_returns,
                as_of_dates=eligible,
            )
            n_written = len(metrics)
            if metrics:
                # eligible 按 todo DESC, 第一个即最新 as_of。
                latest_metric = metrics[0]
        except Exception as exc:  # noqa: BLE001
            # P0-3: SQLite BUSY / compute 异常 — 整批失败时累加 (与旧逐个 catch 粒度不同,
            # 但 evaluate_batch_fast 内部无逐日 try; 批级失败视为全 eligible 失败)。
            failed_dates.extend(d.isoformat() for d in eligible)
            logger.warning("history_backfill: evaluate_batch_fast(%s) failed: %s",
                           factor.name, exc)

    if n_written == 0 and n_skipped == 0:
        return {"ok": False, "reason": "no_metric_written (insufficient history per as_of_date)",
                "n_metrics_written": 0, "n_skipped": 0,
                "n_failed": len(failed_dates), "failed_dates": failed_dates[:10],
                "latest_ic_mean": None, "latest_ir": None, "latest_t_stat": None}

    # P1-4 (review): fast 模式也 mark, 避免 evaluator._read_recent_history 看到"未来"
    # row 把刚写入的行错标 inactive。强 active 不伤 — 真 inactive 由 _check_factor_decay
    # 监控覆盖。
    if todo:
        _mark_backfill_status(evaluator, factor.name, [d for d, _ in todo])

    # 拿到最新 metric: 优先用本次写入的; 没写入 (全 skip) 时从 db 拿
    if latest_metric is None:
        latest_metric = _read_latest_metric_from_db(evaluator, factor.name, factor.factor_version)

    if proposal_store is not None and latest_metric is not None:
        _sync_proposal(proposal_store, factor.name, latest_metric)

    return {
        "ok": True,
        "n_metrics_written": n_written,
        "n_skipped": n_skipped,
        "n_failed": len(failed_dates),  # P0-3: 上层据此 write events
        "failed_dates": failed_dates[:10],  # 截断防 event payload 过大
        "latest_ic_mean": latest_metric.ic_mean if latest_metric else None,
        "latest_ir": latest_metric.ir if latest_metric else None,
        "latest_t_stat": latest_metric.t_stat if latest_metric else None,
        "reason": None,
    }


def _read_existing_metric_dates(evaluator: Any, factor_name: str, factor_version: int) -> set[str]:
    """读 db 里该 factor 已有的 as_of_date ISO 集合, 用于 fast 模式 skip 已算."""
    db_path = getattr(evaluator, "_db", None)
    window = getattr(evaluator, "_window", 60)
    if db_path is None:
        return set()
    try:
        with open_meta_db(db_path) as conn:
            rows = conn.execute(
                "SELECT as_of_date FROM factor_metrics "
                "WHERE factor_name=? AND factor_version=? AND window_days=?",
                (factor_name, factor_version, window),
            ).fetchall()
        return {r[0] for r in rows}
    except Exception as exc:  # noqa: BLE001
        logger.debug("_read_existing_metric_dates(%s) failed: %s", factor_name, exc)
        return set()


def _read_latest_metric_from_db(evaluator: Any, factor_name: str, factor_version: int):
    """fast 模式下全部 skip 时, 从 db 取最新一期同步给 factor_proposals."""
    try:
        return evaluator.get_latest(factor_name, factor_version)
    except Exception:  # noqa: BLE001
        return None


def _default_compute_factor_history(factor, ohlcv, all_dates):
    """fallback: 复用 DiscoveryEngine 的逻辑, 此处直接重新写一份避免循环 import.

    DiscoveryEngine._compute_factor_history 同款语义: 每个 as_of_date 用截止那日的子集
    跑 factor.compute, 失败 / 数据不足跳过。
    """
    rows = {}
    for d in all_dates:
        d_date = d.date() if hasattr(d, "date") else d
        sub = ohlcv[ohlcv["date"] <= d_date]
        if len(sub) < factor.lookback_days:
            continue
        try:
            s = factor.compute(sub)
        except Exception:  # noqa: BLE001
            continue
        if s is None or s.empty:
            continue
        rows[d] = s
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).T


def _mark_backfill_status(evaluator: Any, factor_name: str, as_of_dates) -> None:
    """把这批 backfill 写的行 reason 改成 'backfill', status 强 active.

    evaluator.evaluate 内部 _read_recent_history 在 backfill 场景看到"未来"row,
    会错算 low_ir_persistent 标 inactive (污染 registry.list_active).
    """
    db_path = getattr(evaluator, "_db", None)
    if db_path is None:
        return
    dates_iso = [(d.date() if hasattr(d, "date") else d).isoformat() for d in as_of_dates]
    if not dates_iso:
        return
    placeholders = ",".join(["?"] * len(dates_iso))
    try:
        with open_meta_db(db_path) as conn:
            conn.execute(
                f"UPDATE factor_metrics SET status='active', reason='backfill' "
                f"WHERE factor_name=? AND as_of_date IN ({placeholders})",
                (factor_name, *dates_iso),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("_mark_backfill_status(%s) failed: %s", factor_name, exc)


def _sync_proposal(proposal_store: Any, factor_name: str, latest_metric: Any) -> None:
    """把 latest_metric 的 ic/ir/t_stat 同步到 factor_proposals.

    场景: 用户在 web /research 看「自动发现因子流水」, 这张表读 factor_proposals.ir/ic_mean.
    LLM brainstorm 入库 status='llm_suggested' 时这俩字段是 NULL, 用户没法做接受决策。
    backfill 完同步一次, 表里立刻有数。
    """
    try:
        db_path = getattr(proposal_store, "_db", None)
        if db_path is None:
            return
        with open_meta_db(db_path) as conn:
            conn.execute(
                """
                UPDATE factor_proposals
                SET ic_mean = ?, ic_std = ?, ir = ?, t_stat = ?
                WHERE factor_name = ?
                """,
                (
                    latest_metric.ic_mean,
                    latest_metric.ic_std,
                    latest_metric.ir,
                    latest_metric.t_stat,
                    factor_name,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("_sync_proposal(%s) failed: %s", factor_name, exc)
