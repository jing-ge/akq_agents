"""``batch.deep_research``：每日盘后跑因子有效性滚动评估。

评估对象（用户需求："因子没有入选也应该每天评估，没准某天就适用了"）：
- ``factor_registry.list_all()`` — builtin + accepted (active 因子)
- ``factor_proposals`` 里 status in (shadow, accepted, rejected, demoted) 的因子
  跳过 ``rejected.reason='compute_error'``（recipe 跑不出值的死因子, 算也是 NULL）

对每个因子：
- 拉过去 max(lookback)+60+10 天 OHLCV
- 按日跑因子计算 → factor_history (index=date, columns=symbol)
- 下一日收益 forward_returns = close.pct_change().shift(-1)
- 调 ``FactorEvaluator.evaluate`` 计算 IC/IR/t-stat 并写 factor_metrics 表

注意：本任务跑得慢（O(N_factors × N_days × N_symbols)），但 builtin + shadow + rejected
合在一起可能 ~200 个因子。timeout_s 给 5400s 兜底；首跑可能慢, 后续按 cron 每日重跑。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "batch.deep_research"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册到 APScheduler。需要 services 提供 data_repository / factor_registry /
    factor_evaluator / factor_proposal_store。缺任一关键依赖时即便 enabled=True 也跳过注册。"""
    job_cfg = cfg.jobs.batch_deep_research
    if not job_cfg.enabled:
        return
    if not _has_required_services(services):
        logger.info("batch.deep_research enabled but services missing; skip registration")
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)

    trigger_kwargs: dict[str, Any] = {
        "hour": job_cfg.hour,
        "minute": job_cfg.minute,
    }
    if job_cfg.day_of_week:
        trigger_kwargs["day_of_week"] = job_cfg.day_of_week

    scheduler.add_job(
        _run,
        CronTrigger(**trigger_kwargs),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
    )


def _has_required_services(services: dict[str, Any]) -> bool:
    # factor_proposal_store 是新依赖；老部署没装时退化只评估 registry（向后兼容）
    return all(k in services for k in {"data_repository", "factor_registry", "factor_evaluator"})


def _do(services: dict[str, Any], *, mode: str = "fast") -> dict[str, Any]:
    """实际业务：对每个 factor 做 rolling IC 评估并写表。

    Args:
        mode: 'fast' (默认, 跳过 db 已有日期, 只补缺失 — 日常 cron / 用户日常按) /
            'full' (重算所有 90 天 覆盖 db — 数据修复或主动重算用)
    """
    import pandas as pd

    repo = services["data_repository"]
    registry = services["factor_registry"]
    evaluator = services["factor_evaluator"]
    proposal_store = services.get("factor_proposal_store")

    today = date.today()
    # M19-A: 凌晨 / 盘前 today 数据还没刷, get_universe 会抛 DataNotReady.
    # fallback 用昨天的 universe (历史滚动评估对 universe 精确性要求不高).
    try:
        universe = repo.get_universe(today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch.deep_research: get_universe(today) failed: %s; fallback to today-1", exc)
        try:
            universe = repo.get_universe(today - timedelta(days=1))
        except Exception as exc2:  # noqa: BLE001
            return {"factors_evaluated": 0, "reason": f"no_universe: {exc2}"}
    full_symbols = universe.symbols

    # 限制 universe 减少计算量：取 top 500 by 流动性，与 PortfolioAgent 一致
    from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

    # 1) 汇总评估对象 (builtin + accepted + shadow + 历史 rejected/demoted, 跳过 compute_error)
    evaluation_targets = _collect_targets(registry, proposal_store)
    if not evaluation_targets:
        return {"factors_evaluated": 0, "reason": "no_factors"}

    # 2) 拉一段 OHLCV 用于评估
    max_lookback = max((f.lookback_days for f in evaluation_targets), default=80)
    window = evaluator._window if hasattr(evaluator, "_window") else 60
    history_days = max_lookback + window + 10
    start = today - timedelta(days=history_days * 2)
    ohlcv = repo.get_ohlcv_loose(full_symbols, start, today)
    if ohlcv.empty:
        # 凌晨/盘前 today 可能还没数据；fallback 用 today-1
        ohlcv = repo.get_ohlcv_loose(full_symbols, start, today - timedelta(days=1))
    if ohlcv.empty:
        return {"factors_evaluated": 0, "reason": "no_data", "n_targets": len(evaluation_targets)}
    portfolio_universe = build_portfolio_universe(
        full_universe_symbols=full_symbols, ohlcv=ohlcv, top_n=500, window=20
    )
    sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(portfolio_universe))]
    # close pivot 一次性
    close = sub_ohlcv.pivot_table(
        index="date", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    # fill_method=None: 停牌日不要 pad 填充, 避免把停牌天 return 算成 0 稀释 IC
    forward_returns = close.pct_change(fill_method=None).shift(-1)

    # M19: 重写 — ThreadPoolExecutor 4 worker 并行 + 调公共 backfill_one 写 90 天历史。
    # 之前: 串行 233 因子 × 6s = 23 分钟, 且每因子只写 1 行 today metric (历史断断续续)。
    # 现在: 4 worker 并行 ~6 min, 每因子写 90 行历史让 IC 曲线连续。
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from akq_agents.services.factors.history_backfill import (
        HistoryBackfillContext,
        backfill_one,
    )

    bf_ctx = HistoryBackfillContext.from_existing(
        ohlcv=sub_ohlcv,
        close=close,
        forward_returns=forward_returns,
        window=window,
        days=90,
        step=1,
        as_of_date=today,
    )
    if bf_ctx is None:
        return {"factors_evaluated": 0, "reason": "ctx_build_failed",
                "n_targets": len(evaluation_targets)}

    # 内部 _compute_factor_history (避免循环 import; 跟 history_backfill 默认实现一致)
    def _compute_fh(factor, ohlcv_arg, all_dates_arg):
        rows = {}
        for d in all_dates_arg:
            d_date = d.date() if hasattr(d, "date") else d
            sub = ohlcv_arg[ohlcv_arg["date"] <= d_date]
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

    metrics_written = 0
    failures = 0

    def _process_one(factor):
        try:
            r = backfill_one(
                factor, bf_ctx,
                evaluator=evaluator,
                proposal_store=proposal_store,
                compute_factor_history=_compute_fh,
                mode=mode,
            )
            return (factor.name, r.get("ok", False),
                    r.get("n_metrics_written", 0), r.get("n_skipped", 0),
                    r.get("n_failed", 0), r.get("failed_dates", []),
                    r.get("reason"))
        except Exception as exc:  # noqa: BLE001
            return (factor.name, False, 0, 0, 0, [], f"exception: {exc}")

    # 4 worker — pandas/numpy 大量 C 层释放 GIL, ThreadPool 有效。SQLite WAL 写并发
    # 由 evaluator._upsert 各自 open_meta_db 处理 (短事务, 写互斥但读不阻塞)。
    total_written = 0
    total_skipped = 0
    total_failed_dates = 0  # P0-3: 累加因 SQLite BUSY / compute 异常等真失败的行数
    factors_with_failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="deep-research") as ex:
        futures = [ex.submit(_process_one, f) for f in evaluation_targets]
        for fut in as_completed(futures):
            try:
                name, ok, n_w, n_s, n_f, failed_dates, reason = fut.result()
                total_written += n_w
                total_skipped += n_s
                total_failed_dates += n_f
                if n_f > 0:
                    factors_with_failures.append({
                        "factor": name,
                        "n_failed": n_f,
                        "failed_dates": failed_dates,
                    })
                if ok and (n_w > 0 or n_s > 0):
                    metrics_written += 1
                else:
                    failures += 1
                    if reason:
                        logger.debug("batch.deep_research: %s skipped: %s", name, reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("batch.deep_research: future raised: %s", exc)
                failures += 1

    # P0-3: 真有失败行的话 写 events 让 ops 看板可见
    if total_failed_dates > 0:
        state_store = services.get("scheduler_state_store")
        if state_store is not None:
            try:
                state_store.write_event(
                    level="warning",
                    kind="factor.evaluate_failed_rows",
                    source="batch.deep_research",
                    payload={
                        "total_failed_dates": total_failed_dates,
                        "n_factors_with_failures": len(factors_with_failures),
                        "sample": factors_with_failures[:5],  # 前 5 个截断防过大
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    return {
        "factors_evaluated": metrics_written,
        "factors_failed": failures,
        "n_targets": len(evaluation_targets),
        "window": window,
        "mode": mode,
        "rows_written": total_written,
        "rows_skipped": total_skipped,
        "rows_failed": total_failed_dates,  # P0-3: 真失败行数 (SQLite BUSY / 异常)
    }


def _collect_targets(registry: Any, proposal_store: Any) -> list[Any]:
    """汇总要评估的因子: builtin + accepted (registry) + shadow + 历史 rejected/demoted (proposal_store).

    跳过 ``rejected.reason='compute_error'`` — recipe 跑不出值, 评估也是 NULL。

    去重逻辑: registry 优先(builtin 用手写 Factor 类), 再补 proposal_store 里 registry 没有的。
    """
    from akq_agents.services.factors.discovery import make_factor
    from akq_agents.services.factors.proposal_store import recipe_from_json

    targets: list[Any] = []
    seen: set[str] = set()

    # 1) registry 里的所有 active 因子 (builtin + 已 promoted 的 accepted)
    for f in registry.list_all():
        if f.name in seen:
            continue
        seen.add(f.name)
        targets.append(f)

    # 2) proposal_store 里还没在 registry 的 factor (shadow / 历史 rejected / demoted)
    if proposal_store is not None:
        try:
            # 拿全部, 自己过滤 status & reason
            with _open_proposal_db(proposal_store) as conn:
                rows = conn.execute(
                    """
                    SELECT factor_name, recipe_json, status, reason
                    FROM factor_proposals
                    WHERE status IN ('shadow', 'accepted', 'rejected', 'demoted')
                    """
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch.deep_research: read proposal_store failed: %s", exc)
            rows = []

        for name, recipe_json, status, reason in rows:
            if name in seen:
                continue
            # 跳过 compute_error 类 rejected (recipe 死的, 评估也是 NULL)
            if status == "rejected" and reason and reason.startswith("compute_error"):
                continue
            try:
                recipe = recipe_from_json(recipe_json)
                factor = make_factor(recipe)
            except Exception as exc:  # noqa: BLE001
                logger.debug("batch.deep_research: make_factor failed for %s: %s", name, exc)
                continue
            # 用 proposal 里的 factor_name (含 hash 后缀), 不要被 make_factor 重新算的名字覆盖
            try:
                factor.name = name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            seen.add(name)
            targets.append(factor)

    return targets


def _open_proposal_db(proposal_store: Any):
    """从 FactorProposalStore 取数据库连接 (复用其私有 _db 路径, 避免再算路径)."""
    from akq_agents.services.data.repository import open_meta_db

    return open_meta_db(proposal_store._db)
