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
    # M19 review: 周末 / 节假日 / 盘中 today 数据没刷 — 用 calendar 找最近交易日.
    # 旧逻辑 fallback today-1 跨周末会死.
    cal = getattr(repo, "_calendar", None)
    # M19-A: 凌晨 / 盘前 today 数据还没刷, get_universe 会抛 DataNotReady.
    try:
        universe = repo.get_universe(today)
    except Exception as exc:  # noqa: BLE001
        fb = cal.previous_trading_day(today) if cal is not None else (today - timedelta(days=1))
        logger.warning("batch.deep_research: get_universe(today) failed: %s; fallback to %s", exc, fb)
        try:
            universe = repo.get_universe(fb)
            today = fb  # 同步推进, 让下面 OHLCV 拉同日期段
        except Exception as exc2:  # noqa: BLE001
            return {"factors_evaluated": 0, "reason": f"no_universe: {exc2}"}
    full_symbols = universe.symbols

    # 限制 universe 减少计算量:取 top 500 by 流动性, 与 PortfolioAgent 一致
    from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

    # 1) 汇总评估对象 (builtin + accepted + shadow + 历史 rejected/demoted, 跳过 compute_error)
    evaluation_targets = _collect_targets(registry, proposal_store)
    if not evaluation_targets:
        return {"factors_evaluated": 0, "reason": "no_factors"}

    logger.info(
        "batch.deep_research: START mode=%s n_targets=%d as_of=%s universe=%d",
        mode, len(evaluation_targets), today.isoformat(), len(full_symbols),
    )

    # 2) 拉一段 OHLCV 用于评估
    max_lookback = max((f.lookback_days for f in evaluation_targets), default=80)
    window = evaluator._window if hasattr(evaluator, "_window") else 60
    history_days = max_lookback + window + 10
    start = today - timedelta(days=history_days * 2)
    ohlcv = repo.get_ohlcv_loose(full_symbols, start, today)
    if ohlcv.empty:
        # 凌晨 / 盘前 / 周末 today 可能还没数据; fallback 用上一交易日
        try:
            prev_d = cal.previous_trading_day(today) if cal is not None else (today - timedelta(days=1))
            ohlcv = repo.get_ohlcv_loose(full_symbols, start, prev_d)
        except Exception:  # noqa: BLE001
            pass
    if ohlcv.empty:
        return {"factors_evaluated": 0, "reason": "no_data", "n_targets": len(evaluation_targets)}
    portfolio_universe = build_portfolio_universe(
        full_universe_symbols=full_symbols, ohlcv=ohlcv, top_n=500, window=20
    )
    sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(portfolio_universe))]
    # close pivot 一次性; fill_method=None: 停牌日不要 pad 填充, 避免把停牌天 return 算成 0 稀释 IC
    from akq_agents.services.factors.base import compute_forward_returns, pivot_close_wide

    close = pivot_close_wide(sub_ohlcv)
    forward_returns = compute_forward_returns(close)

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

    logger.info(
        "batch.deep_research: ctx ready portfolio_universe=%d ohlcv_rows=%d date_range=[%s .. %s] window=%d days=90",
        len(portfolio_universe), len(sub_ohlcv),
        str(close.index.min())[:10] if not close.empty else "-",
        str(close.index.max())[:10] if not close.empty else "-",
        window,
    )

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

    # worker 4 平衡: macOS 物理核 ~8-10, 4 worker 保证 web 进程在 force_full 路径下
    # 仍有 ~4-6 核空闲给 event loop / SQLite reader. 之前 M22 提到 8 worker 在 daemon
    # 进程跑 cron 任务没问题 (905s/15min) — 但当 force_full 走 web 进程 (1 worker outer
    # pool) 时, 8 worker inner 把 web 进程 CPU 吃到 800%+, 5s data-freshness 端点超时
    # → 整站不可用. 改回 4 worker 是 web/daemon 双进程兼容方案.
    # 性能: 4 worker 比 8 worker 慢 ~30%, 但仍比 M22 前的 90 次单事务 commit 路径快得多
    # (evaluator._upsert_many 一次事务写 90 天, commit 从 18,000 -> 200 锁争用降 ~90x).
    total_written = 0
    total_skipped = 0
    total_failed_dates = 0  # P0-3: 累加因 SQLite BUSY / compute 异常等真失败的行数
    factors_with_failures: list[dict[str, Any]] = []

    # M22: 进度上报 - 每 STEP_BATCH 个因子完成就写一行 job_steps, UI 轮询可见.
    # 小批量任务 (n_targets < STEP_BATCH) 会在 done_count == n_targets 时写最后一行, 这是兜底,
    # 保证用户最少看到 1 行 "完成" 进度.
    import time as _time

    from akq_agents.orchestrator.step_recorder import StepRecorder
    STEP_BATCH = 10  # M22: 20 -> 10, 让 n_candidates 10-20 的小批量也看到中间进度
    repo_path = services.get("data_repository")
    recorder: StepRecorder | None = None
    if repo_path is not None and hasattr(repo_path, "_base_dir"):
        try:
            recorder = StepRecorder(
                repo_path.meta_db_path,
                parent_job_id="batch.deep_research",
                parent_partition=str(date.today().isoformat()),
            )
        except Exception:  # noqa: BLE001
            recorder = None
    n_targets = len(evaluation_targets)
    t_start = _time.monotonic()
    done_count = 0
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="deep-research") as ex:
        futures = {ex.submit(_process_one, f): f for f in evaluation_targets}
        for fut in as_completed(futures):
            try:
                name, ok, n_w, n_s, n_f, failed_dates, reason = fut.result()
                total_written += n_w
                total_skipped += n_s
                total_failed_dates += n_f
                done_count += 1
                # 日志: 每 STEP_BATCH 或到终点时打一行进度到 /logs "回测/因子重算" 源.
                # 无论 recorder 是否装配都要打(recorder 只影响 job_steps 表, 日志走独立通道).
                if done_count % STEP_BATCH == 0 or done_count == n_targets:
                    elapsed = _time.monotonic() - t_start
                    rate = done_count / elapsed if elapsed > 0 else 0
                    eta_s = (n_targets - done_count) / rate if rate > 0 else None
                    logger.info(
                        "batch.deep_research: progress %d/%d (%.1f%%) rate=%.2f/s elapsed=%.0fs eta=%s rows_written=%d rows_skipped=%d rows_failed=%d",
                        done_count, n_targets,
                        100.0 * done_count / n_targets if n_targets else 0.0,
                        rate, elapsed,
                        f"{eta_s:.0f}s" if eta_s else "-",
                        total_written, total_skipped, total_failed_dates,
                    )
                    if recorder is not None:
                        try:
                            with recorder.step(
                                f"batch{done_count // STEP_BATCH}",
                                payload_in={
                                    "done": done_count,
                                    "total": n_targets,
                                    "rate_per_s": round(rate, 3),
                                    "elapsed_s": round(elapsed, 1),
                                    "eta_s": round(eta_s, 1) if eta_s else None,
                                    "rows_written_so_far": total_written,
                                },
                            ):
                                pass
                        except Exception:  # noqa: BLE001
                            logger.exception("batch.deep_research: recorder.step failed at done_count=%d", done_count)
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

    logger.info(
        "batch.deep_research: DONE mode=%s targets=%d evaluated=%d failed=%d rows_written=%d rows_skipped=%d rows_failed=%d elapsed=%.1fs",
        mode, len(evaluation_targets), metrics_written, failures,
        total_written, total_skipped, total_failed_dates,
        _time.monotonic() - t_start,
    )

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
