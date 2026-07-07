"""``manual_trigger_picker``: M23 — web → daemon 手动触发通道的 picker.

背景 (2026-07-01 incident):
之前 force_full=True 走 web 进程 ``svc.job_runner.submit()`` 把 batch.deep_research
8 worker 池放进 web 进程. 双 manual 并发直接把 web 进程 CPU 吃到 800%+, data-freshness
端点 5s+ 不返回, 整站不可达.

设计:
- web 端点 POST /jobs/{name}/trigger 写一行 pending_triggers (status='pending') + 写
  job_runs.status='pending', 立即返回 202 + poll_url. 不在 web 进程跑任何业务.
- daemon 周期任务本 picker 每 5s 扫一次, 用 SQLite 原子 UPDATE claim 一行
  (claimed_at=now, claimed_by="manual_trigger_picker"), 然后用 daemon 自己的
  JobRunner.run() 跑 (走 daemon 4 worker pool + batch_deep_research 内部 8 worker,
  全在 daemon 进程内, web 进程零 CPU 消耗).
- 跑完写 job_runs.status=ok/failed + mark_trigger_finished. 失败行留作 audit,
  retention 走 cleanup() 删老行 (保留 7 天).

并发安全:
- claim_one_pending_trigger 用单条 UPDATE...WHERE status='pending' RETURNING 原子操作,
  多 daemon / 重启 daemon 都不会重复触发同一行.
- control.py trigger 时调 has_pending_or_running_for_job() 阻止用户连点导致 N 行
  排队 (picker 是 FIFO 单线程).

dispatch 表 (job_id → 业务函数 + timeout):
- batch.post_close: 跑 batch_post_close._do, 透传 ws_services (含 workflow, recorder)
- batch.deep_research: 跑 batch_deep_research._do, mode='fast' or 'full' 来自 payload
- factor.discovery: 跑 discovery_engine.run_batch, n_candidates 来自 payload
- factor.eviction: 跑 factor_eviction._do, dry_run 来自 payload

M24 user-facing job 表 (job_id → 业务函数, **结果写到 job_results**):
- factor.backtest_single: 单因子 90 天组合回测, payload={factor_name, days, rebalance_step, top_n}
- factor.backtest_single: 单因子回测, payload={factor_name, days, rebalance_step, top_n}
- portfolio.trade_list_recompute: 重算今日 trade_list, payload={}
- portfolio.nav_rebuild: 全历史 NAV 回填, payload={}
这 4 个业务跑完把 result dict 写到 job_results 表, 前端 GET /jobs/{name}/{partition}/result
按 partition 轮询. 跟其他 cron job 走的是"完成即可"模式不同, 这 4 个前端要立刻拿数据画图.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

logger = logging.getLogger(__name__)

JOB_ID = "manual.trigger_picker"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册 picker 周期任务. 跟其他 cron/interval job 一样, max_instances=1 防重叠."""
    job_cfg = cfg.jobs.manual_trigger_picker
    if not job_cfg.enabled:
        return

    # picker 自身不开新 thread, 走 JobRunner 内部 4 worker pool (跟 cron job 同队列).
    # max_instances=1 保证同时只有一个 picker 跑, 不会出现"两个 picker 抢同一行"的竞态
    # (虽然 claim 是原子的不会出错, 但单实例省去无谓的 SQL 竞争).
    scheduler.add_job(
        lambda: _tick(runner, services, job_cfg.timeout_s),
        IntervalTrigger(seconds=job_cfg.interval_seconds),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
        # 不在 trading_day 白名单 — web 触发不依赖交易日, 周末/节假日也跑.
    )


def _tick(runner: JobRunner, services: dict[str, Any], timeout_s: int) -> None:
    """每 interval_seconds 调一次: claim 一行 → dispatch (fire-and-forget) → 立即返回.

    重构 (fix picker 阻塞导致 APScheduler max_instances=1 告警):
    - 老实现: _dispatch 内部走 runner.run() 同步等 result, 90 天 IS-IC backfill
      期间 tick 阻塞 30-90s, 期间新 pending_triggers 无法 claim, 且 APScheduler
      每 5s tick 全部被 max_instances=1 挡掉刷 WARNING.
    - 新实现: _dispatch 内部改走 runner.submit() 立即返回 future; 单次 tick 只做
      claim + 提交, 毫秒级完成. mark_trigger_finished 挂到 future.done_callback
      里, 业务完成后再标 ok/failed. 用户可最多同时跑 4 个 (JobRunner pool size).
    """
    store = services.get("scheduler_state_store")
    if store is None:
        logger.warning("manual_trigger_picker: scheduler_state_store missing in services, skip")
        return

    claimed = store.claim_one_pending_trigger(claimed_by=JOB_ID)
    if claimed is None:
        return  # 没 pending 行, 安静返回

    trigger_id = claimed["id"]
    job_id = claimed["job_id"]
    partition = claimed["partition"]
    payload = claimed["payload"] or {}

    logger.info(
        "manual_trigger_picker: claim trigger_id=%d job_id=%s partition=%s payload=%s",
        trigger_id, job_id, partition, payload,
    )

    try:
        _dispatch(runner, services, store, job_id, partition, payload, timeout_s, trigger_id)
    except Exception:  # noqa: BLE001 — dispatch 阶段抛异常 (发生在 submit 之前, 不常见)
        logger.exception("manual_trigger_picker: dispatch failed for trigger_id=%d", trigger_id)
        _finalize_trigger(store, trigger_id, status="failed")
        return
    # 成功路径: dispatch 已经把业务 submit 到 worker pool, mark_trigger_finished
    # 会由 future done_callback 在业务完成时触发, 不在此处同步标 ok.


def _finalize_trigger(store: Any, trigger_id: int, *, status: str) -> None:
    """把 pending_triggers 行标 ok/failed. 供 done_callback 和 dispatch 兜底共用."""
    try:
        store.mark_trigger_finished(trigger_id, status=status)
    except Exception:  # noqa: BLE001
        logger.warning(
            "manual_trigger_picker: mark trigger_id=%d status=%s failed", trigger_id, status,
        )


def _attach_finalize_callback(
    future: Any, store: Any, trigger_id: int, job_id: str, partition: str,
) -> None:
    """给 runner.submit 返回的 future 挂 done_callback:
    业务跑完后按 result.ok / 异常状态回写 pending_triggers.status.

    submit 内部 _wrapped 已经吞了业务异常并写 job_runs.status='failed', future
    本身几乎不会抛; 但仍保留兜底 try/except.
    """
    def _cb(fut: Any) -> None:
        try:
            result = fut.result()
        except Exception:  # noqa: BLE001 — future 本身抛 (submit 内部逻辑异常 / cancel)
            logger.exception(
                "manual_trigger_picker: future crashed for trigger_id=%d job_id=%s partition=%s",
                trigger_id, job_id, partition,
            )
            _finalize_trigger(store, trigger_id, status="failed")
            return
        # result 是 JobRunResult (submit 路径) 或 dict (兼容). 统一按 status 判定.
        status = getattr(result, "status", None) if result is not None else None
        final = "ok" if status in ("ok", "noop") else "failed"
        _finalize_trigger(store, trigger_id, status=final)

    try:
        future.add_done_callback(_cb)
    except Exception:  # noqa: BLE001 — future 已完成时 add_done_callback 立即触发, 不该抛
        logger.exception(
            "manual_trigger_picker: attach done_callback failed for trigger_id=%d", trigger_id,
        )
        # 兜底: future 走不到 callback 就手动标 ok (业务已提交, 大概率会跑完)
        _finalize_trigger(store, trigger_id, status="ok")


def _dispatch(
    runner: JobRunner,
    services: dict[str, Any],
    store: Any,
    job_id: str,
    partition: str,
    payload: dict[str, Any],
    timeout_s: int,
    trigger_id: int,
) -> None:
    """根据 job_id submit 到 runner 4-worker pool, 立即返回. mark_trigger_finished
    挂在 future done_callback 里, 业务完成时触发.

    partition 是 manual-xxxxxx 唯一, 不会跟 cron 同 partition 撞, 也不会跟其他
    manual 行撞, 所以 runner.submit 的幂等检查 (ALREADY_OK) 只会在极端重试场景命中.

    M24: 4 个 user-facing job 走 _run_user_facing_job (需要 store 写 job_results).
    """
    if job_id in _USER_FACING_JOBS:
        _run_user_facing_job(runner, services, store, job_id, partition, payload, timeout_s, trigger_id)
        return

    if job_id == "batch.post_close":
        _run_batch_post_close(runner, services, store, partition, timeout_s, trigger_id)
    elif job_id == "batch.deep_research":
        _run_batch_deep_research(runner, services, store, partition, payload, timeout_s, trigger_id)
    elif job_id == "factor.discovery":
        _run_factor_discovery(runner, services, store, partition, payload, timeout_s, trigger_id)
    elif job_id == "factor.eviction":
        _run_factor_eviction(runner, services, store, partition, payload, timeout_s, trigger_id)
    else:
        logger.error(
            "manual_trigger_picker: unknown job_id=%s (trigger payload=%s), skip",
            job_id, payload,
        )
        # 未知 job_id 也要 finalize, 否则 pending_triggers 行永久 claimed
        _finalize_trigger(store, trigger_id, status="failed")


# M24: user-facing job — 业务跑完结果要直接给前端用 (backtest NAV / brainstorm 建议 /
# trade_list items / nav full history), 不写 job_results 前端就拿不到数据.
# (DSL 受限的 factor.brainstorm 已下线, 用 factor.code_brainstorm 走自由代码路径)
_USER_FACING_JOBS: frozenset[str] = frozenset({
    "factor.backtest_single",
    "factor.code_brainstorm",
    "factor.llm_accept",
    "portfolio.trade_list_recompute",
    "portfolio.nav_rebuild",
})


def _run_user_facing_job(
    runner: JobRunner, services: dict[str, Any], store: Any,
    job_id: str, partition: str, payload: dict[str, Any], timeout_s: int,
    trigger_id: int,
) -> None:
    """M24 + fire-and-forget 重构: 4 (+1) 个 user-facing job 走 runner.submit,
    立即返回, 由 worker pool 跑业务, done_callback 挂 mark_trigger_finished.

    流程:
    1) 构造 _do: 调对应业务函数返回 dict, 顺便写 job_results (前端 GET 端点读).
    2) runner.submit(job_id, partition, _do, timeout_s=...) — pool worker 跑,
       job_runs.status='running/ok/failed' 由 runner._finish_run 统一管.
    3) 给返回的 future 挂 _finalize_trigger 回调, 业务完成后再标 pending_triggers.
    4) _tick 立即返回, 不再阻塞 APScheduler 5s tick.

    去掉了老实现里三层重复写 job_runs / event 的代码 (老代码里 picker 自己写一次,
    _do 内部再写一次, runner.run 里 _executor 完成后又写一次).
    """
    def _do() -> dict[str, Any]:
        try:
            if job_id == "factor.backtest_single":
                result = _do_factor_backtest_single(services, payload)
            elif job_id == "factor.code_brainstorm":
                result = _do_factor_code_brainstorm(services, payload)
            elif job_id == "factor.llm_accept":
                result = _do_factor_llm_accept(services, payload)
            elif job_id == "portfolio.trade_list_recompute":
                result = _do_portfolio_trade_list_recompute(services, payload)
            elif job_id == "portfolio.nav_rebuild":
                result = _do_portfolio_nav_rebuild(services, payload)
            else:
                result = {"ok": False, "error": f"unknown user-facing job: {job_id}"}
        except Exception as exc:  # noqa: BLE001 — 业务异常不能让 runner._wrapped 崩
            logger.exception("user-facing job %s crashed", job_id)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # 写 result (前端 GET 端点读这) — 无论 ok/failed 都写, 前端能拿到错误信息
        try:
            store.set_job_result(job_id, partition, result)
        except Exception:  # noqa: BLE001
            logger.exception("set_job_result failed for %s/%s", job_id, partition)

        # 让 runner._finish_run 按 result.ok 决定 job_runs 状态.
        # _finish_run 在 payload 是 dict 时会写进 job_runs.payload, event 也用它.
        # 业务失败但函数正常返回 (ok=False) → 我们把 result 转成 raise 以让 _finish_run
        # 写 status='failed'? 不, 保持"业务函数返回 dict + ok bool"契约: 我们靠
        # _run_user_facing_job 判断 result.ok 是否 True, 若 False 就在这里 raise
        # 让 _finish_run 走 failed 分支.
        if not result.get("ok"):
            raise _UserFacingJobFailed(result.get("error_code") or "USER_FACING_FAILED", result)
        return result

    fut = runner.submit(job_id, partition, _do, timeout_s=timeout_s)
    _attach_finalize_callback(fut, store, trigger_id, job_id, partition)


class _UserFacingJobFailed(Exception):
    """业务函数返回 ok=False 时抛这个, 让 runner._finish_run 写 status='failed'.

    reason_code 通过 exception 的第一个 arg 传递; runner._classify_exception
    会把 UNKNOWN 类映射到 'failed'. result payload 也带上供日志.
    """
    def __init__(self, reason_code: str, result: dict[str, Any]) -> None:
        super().__init__(f"{reason_code}: {result.get('error') or ''}")
        self.reason_code = reason_code
        self.result = result


# ============== 4 个 user-facing 业务的实现 (从 web 端点搬过来) ==============


def _resolve_factor_by_name(services: dict[str, Any], name: str) -> Any | None:
    """从 registry 或 proposal_store 反解一个 Factor 实例.

    直接复用 web 端 _resolve_factor_by_name 的逻辑 (搬到 daemon 进程跑).
    """
    registry = services.get("factor_registry")
    if registry is not None:
        for f in registry.list_all():
            if f.name == name:
                return f
    proposal_store = services.get("factor_proposal_store")
    repo = services.get("data_repository")
    if proposal_store is None or repo is None:
        return None
    try:
        from akq_agents.services.data.repository import open_meta_db
        from akq_agents.services.factors.discovery import make_factor
        from akq_agents.services.factors.proposal_store import recipe_from_json
        db_path = repo.meta_db_path
        with open_meta_db(db_path) as conn:
            row = conn.execute(
                "SELECT recipe_json FROM factor_proposals WHERE factor_name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        recipe = recipe_from_json(row[0])
        factor = make_factor(recipe)
        factor.name = name  # type: ignore[attr-defined]
        return factor
    except Exception:  # noqa: BLE001
        return None


def _do_factor_backtest_single(services: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """单因子组合回测 (从 research.py:backtest_single_factor 搬过来).

    payload: {factor_name, days, rebalance_step, top_n}
    业务量大 (90 天因子 history + backtest), 必走 daemon.
    """
    name = str(payload.get("factor_name", ""))
    days = int(payload.get("days", 90))
    rebalance_step = int(payload.get("rebalance_step", 5))
    top_n = int(payload.get("top_n", 50))
    if not name:
        return {"ok": False, "error": "factor_name missing", "error_code": "BAD_PAYLOAD"}

    engine = services.get("discovery_engine")
    backtester = services.get("portfolio_backtester")
    repo = services.get("data_repository")
    if engine is None or backtester is None or repo is None:
        return {"ok": False, "error": "services not ready (engine/backtester/repo missing)", "error_code": "SERVICES_MISSING"}

    factor = _resolve_factor_by_name(services, name)
    if factor is None:
        return {"ok": False, "factor_name": name, "error": f"factor not found or unmakeable: {name}", "error_code": "FACTOR_NOT_FOUND"}

    from datetime import date as _d

    as_of = _d.today()
    try:
        cal = repo._calendar
        try:
            repo.get_universe(as_of)
        except Exception:  # noqa: BLE001
            as_of = cal.previous_trading_day(as_of)
        if not cal.is_trading_day(as_of):
            as_of = cal.previous_trading_day(as_of)
    except Exception:  # noqa: BLE001
        pass

    try:
        ohlcv, _ = engine._prepare_data(as_of)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"prepare_data failed: {exc}", "error_code": "PREPARE_DATA_FAILED"}
    if ohlcv.empty:
        try:
            prev = repo._calendar.previous_trading_day(as_of)
            ohlcv, _ = engine._prepare_data(prev)  # type: ignore[attr-defined]
            as_of = prev
        except Exception:  # noqa: BLE001
            pass
    if ohlcv.empty:
        return {"ok": False, "error": f"no ohlcv for {as_of}", "error_code": "NO_OHLCV"}

    from akq_agents.services.factors.base import pivot_close_wide
    close = pivot_close_wide(ohlcv)
    all_dates = list(close.index)
    if len(all_dates) < days + 5:
        return {"ok": False, "error": f"insufficient history: only {len(all_dates)} days, need {days+5}", "error_code": "INSUFFICIENT_HISTORY"}

    try:
        factor_history = engine._compute_factor_history(factor, ohlcv, close.index)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"compute_factor_history failed: {exc}", "error_code": "COMPUTE_HISTORY_FAILED"}
    if factor_history is None or factor_history.empty:
        return {"ok": False, "error": "factor_history empty (因子算不出值)", "error_code": "EMPTY_HISTORY"}

    direction = getattr(factor, "direction", "long")
    ascending = (direction == "short")
    weights_by_date: dict[str, dict[str, float]] = {}
    sample_dates = all_dates[-days::rebalance_step]
    for d in sample_dates:
        if d not in factor_history.index:
            continue
        row = factor_history.loc[d].dropna()
        if len(row) < top_n:
            continue
        picks = row.sort_values(ascending=ascending).head(top_n).index.tolist()
        w = 1.0 / len(picks)
        weights_by_date[d.isoformat() if hasattr(d, "isoformat") else str(d)] = {
            sym: w for sym in picks
        }

    if not weights_by_date:
        return {"ok": False, "error": "no valid rebalance dates (因子值缺失或股票不足)", "error_code": "NO_REBALANCE_DATES"}

    try:
        bt_result = backtester.backtest_in_memory(weights_by_date)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"backtest failed: {exc}", "error_code": "BACKTEST_FAILED"}

    if bt_result.nav.empty:
        return {
            "ok": False,
            "factor_name": name,
            "reason": bt_result.summary.get("reason", "empty_nav"),
            "summary": bt_result.summary,
            "error_code": "EMPTY_NAV",
        }

    nav_records = bt_result.nav.to_dict(orient="records")
    return {
        "ok": True,
        "factor_name": name,
        "direction": direction,
        "config": {
            "days": days,
            "rebalance_step": rebalance_step,
            "top_n": top_n,
            "n_rebalances": len(weights_by_date),
        },
        "summary": bt_result.summary,
        "nav": nav_records,
    }


def _do_factor_code_brainstorm(services: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """LLM 自由代码 brainstorm (走 sandbox 编译, 不限 DSL 空间). 手动触发入口.

    payload: {n: int}  默认 10, clamp 到 [1, 30]
    (原 DSL 受限的 _do_factor_brainstorm 已下线, 底层 brainstormer 也统一走 code 路径)
    """
    brainstormer = services.get("llm_code_factor_brainstormer")
    if brainstormer is None:
        return {"ok": False,
                "error": "llm_code_factor_brainstormer not configured (检查 LLM 是否启用)",
                "error_code": "BRAINSTORMER_MISSING"}
    n = int(payload.get("n", 10))
    n = max(1, min(n, 30))
    try:
        stats = brainstormer.run(n=n)
        return {"ok": True, "status": "ok", "stats": stats}
    except Exception as exc:  # noqa: BLE001
        logger.exception("factor.code_brainstorm failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "error_code": "BRAINSTORM_FAILED"}


def _do_factor_llm_accept(services: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """LLM 因子建议"接受": 改 status=shadow + 跑 90 天 IS IC/IR 回填 (从 web research.py 搬过来).

    payload: {factor_name: str}
    之前同步跑在 web 请求里, 90 天 × 全 A 股 pandas 回填重 CPU, 会饿死 web event loop.
    现在整段 (含改 status) 下沉 daemon, web 只入队立即 202.
    """
    from datetime import datetime as _dt

    from akq_agents.services.data.repository import open_meta_db
    from akq_agents.services.factors.discovery import make_factor
    from akq_agents.services.factors.history_backfill import (
        HistoryBackfillContext,
        backfill_one,
    )
    from akq_agents.services.factors.proposal_store import recipe_from_json

    name = str(payload.get("factor_name", ""))
    if not name:
        return {"ok": False, "error": "factor_name missing", "error_code": "BAD_PAYLOAD"}

    repo = services.get("data_repository")
    evaluator = services.get("factor_evaluator")
    engine = services.get("discovery_engine")
    store = services.get("factor_proposal_store")
    if repo is None or evaluator is None or engine is None or store is None:
        return {
            "ok": False,
            "factor_name": name,
            "error": "services not ready (repo/evaluator/engine/proposal_store missing)",
            "error_code": "SERVICES_MISSING",
        }

    db_path = repo.meta_db_path
    ts = _dt.now().isoformat()

    # 1) 校验当前 status 必须是 llm_suggested, 然后改 shadow (幂等: partition manual 唯一, 重跑也安全)
    try:
        with open_meta_db(db_path) as conn:
            row = conn.execute(
                "SELECT status, recipe_json, recipe_kind, recipe_code, code_hash, direction "
                "FROM factor_proposals WHERE factor_name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return {"ok": False, "factor_name": name, "error": f"factor not found: {name}", "error_code": "NOT_FOUND"}
            cur_status, recipe_json, recipe_kind, recipe_code, code_hash, direction = (
                row[0], row[1], row[2], row[3], row[4], row[5],
            )
            # 已被本 job 或其他路径推进过 → 不重复报错, 幂等放行 (仍继续补回填)
            if cur_status == "llm_suggested":
                conn.execute(
                    "UPDATE factor_proposals SET status='shadow', shadow_started_at=?, evaluated_at=? "
                    "WHERE factor_name=?",
                    (ts, ts, name),
                )
                conn.commit()
            elif cur_status != "shadow":
                return {
                    "ok": False,
                    "factor_name": name,
                    "error": f"factor status is {cur_status!r}, not 'llm_suggested'",
                    "error_code": "BAD_STATUS",
                }
    except Exception as exc:  # noqa: BLE001
        logger.exception("factor.llm_accept status update failed for %s", name)
        return {"ok": False, "factor_name": name, "error": f"status update: {exc}", "error_code": "STATUS_UPDATE_FAILED"}

    # 2) 90 天 IS IC/IR 回填 (重 CPU, 这才是下沉 daemon 的主因). 失败不回滚 status —
    #    第二天 batch.deep_research 会补上 metrics, 接受本身已经生效.
    try:
        # 重构: 按 recipe_kind 分支重建 factor.
        # - code 因子: recipe_json 只有 {description, direction}, 真 source 在 recipe_code.
        #   走 compile_code_factor + CodeFactor, 跟 llm_code_brainstorm._backfill_history_for_new_factors 保持一致.
        # - dsl 因子: 老路径, 走 make_factor(recipe).
        if (recipe_kind or "dsl") == "code":
            if not recipe_code or not code_hash:
                return {
                    "ok": True,
                    "factor_name": name,
                    "status": "shadow",
                    "is_ic": {"ok": False, "reason": "code_factor missing recipe_code/code_hash"},
                }
            from akq_agents.services.factors.base import CodeFactor
            from akq_agents.services.factors.sandbox import compile_code_factor
            recipe_meta = recipe_from_json(recipe_json) if recipe_json else {}
            description = recipe_meta.get("description", "")
            fn, ch = compile_code_factor(recipe_code, timeout_s=10.0)
            factor = CodeFactor(
                name=name,
                source_code=recipe_code,
                fn=fn,
                factor_version=1,
                direction=direction or recipe_meta.get("direction", "long"),
                code_hash=ch,
                description=description,
            )
        else:
            recipe = recipe_from_json(recipe_json)
            factor = make_factor(recipe)
            try:
                factor.name = name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        ctx = HistoryBackfillContext.build(repo=repo, evaluator=evaluator, days=90, step=1)
        if ctx is None:
            return {
                "ok": True,
                "factor_name": name,
                "status": "shadow",
                "is_ic": {"ok": False, "reason": "ctx_build_failed (no data?)"},
            }
        result = backfill_one(
            factor, ctx, evaluator=evaluator, proposal_store=store,
            compute_factor_history=engine._compute_factor_history,  # type: ignore[attr-defined]
        )
        return {
            "ok": True,
            "factor_name": name,
            "status": "shadow",
            "is_ic": {
                "ok": bool(result.get("ok")),
                "ic_mean": result.get("latest_ic_mean"),
                "ir": result.get("latest_ir"),
                "t_stat": result.get("latest_t_stat"),
                "n_metrics_written": result.get("n_metrics_written", 0),
                "reason": result.get("reason"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("factor.llm_accept backfill for %s failed: %s", name, exc)
        # status 已改 shadow, 接受生效; 回填失败只是 IC 暂缺
        return {
            "ok": True,
            "factor_name": name,
            "status": "shadow",
            "is_ic": {"ok": False, "reason": f"exception: {exc}"},
        }


def _do_portfolio_trade_list_recompute(services: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """trade_list 重算 (从 trading.py:_recompute_today_trade_list 搬过来).

    业务量: 读 snapshot + 拉 close + generate_trade_list + upsert_cohort. 通常 1-5s.
    payload: {} (无参数, 永远算"最新 snapshot 日期"的那一日).
    """
    workflow = services.get("workflow")
    if workflow is None:
        return {"ok": False, "error": "workflow not ready", "error_code": "WORKFLOW_MISSING"}
    ws = workflow.services
    snap_store = ws.get("portfolio_snapshot_store")
    holdings_store = ws.get("holdings_store")
    tl_store = ws.get("trade_list_store")
    tl_cfg = ws.get("trade_list_config")
    repo = ws.get("data_repository")
    ind_store = ws.get("industry_map_store")
    if not all([snap_store, holdings_store, tl_store, tl_cfg, repo]):
        return {"ok": False, "error": "missing services", "error_code": "SERVICES_MISSING"}

    from datetime import date as _date
    from datetime import timedelta as _td

    import pyarrow.dataset as _ds

    snapshot_dates = snap_store.list_dates(limit=1)
    if not snapshot_dates:
        return {"ok": False, "error": "no snapshots", "error_code": "NO_SNAPSHOTS"}
    target_date = _date.fromisoformat(snapshot_dates[0])
    rows = snap_store.read_snapshot(target_date)
    if not rows:
        return {"ok": False, "error": "no snapshot for today", "error_code": "NO_SNAPSHOT_TODAY"}
    weights = {r.symbol: float(r.weight) for r in rows}
    composite = {r.symbol: float(r.composite_score or 0.0) for r in rows}
    prev_weights_series = snap_store.read_prev_weights(target_date)
    prev_weights = {str(s): float(w) for s, w in prev_weights_series.items()} if not prev_weights_series.empty else {}

    holdings_dict = holdings_store.as_dict()
    all_syms = set(weights.keys()) | set(holdings_dict.keys())
    today_close: dict[str, float] = {}
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    if ohlcv_dir and ohlcv_dir.exists() and all_syms:
        try:
            start = (target_date - _td(days=7)).isoformat()
            end = target_date.isoformat()
            dataset = _ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
            table = dataset.to_table(
                filter=(_ds.field("date") >= start) & (_ds.field("date") <= end) & _ds.field("symbol").isin(list(all_syms)),
                columns=["date", "symbol", "close"],
            )
            df = table.to_pandas()
            if not df.empty:
                df = df.sort_values(["symbol", "date"])
                latest = df.groupby("symbol").tail(1)
                for _, r in latest.iterrows():
                    today_close[str(r["symbol"])] = float(r["close"])
        except Exception as exc:
            logger.warning("close lookup in recompute failed: %s", exc)

    industry_name_map = ind_store.load_names() if ind_store else {}
    from akq_agents.services.portfolio.trade_list import generate_trade_list
    try:
        items = generate_trade_list(
            cohort_date=target_date,
            target_weights=weights,
            current_close=today_close,
            holdings=holdings_dict,
            composite_scores=composite,
            industry_map=industry_name_map,
            yesterday_weights=prev_weights,
            cfg=tl_cfg,
        )
        tl_store.upsert_cohort(target_date, items)
    except Exception as exc:  # noqa: BLE001
        logger.exception("recompute trade_list failed")
        return {"ok": False, "error": f"generate failed: {exc}", "error_code": "GENERATE_FAILED"}
    return {"ok": True, "recomputed": True, "cohort_date": target_date.isoformat(), "n_items": len(items)}


def _do_portfolio_nav_rebuild(services: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """NAV 全历史重建 (从 discovery.py:rebuild_nav 搬过来).

    业务量: 全历史 portfolio 跑一遍 in-sample backtest, 数分钟. 之前会卡 web event loop 数分钟.
    """
    backtester = services.get("portfolio_backtester")
    if backtester is None:
        return {"ok": False, "error": "no_backtester", "error_code": "BACKTESTER_MISSING"}
    try:
        result = backtester.rebuild_full_history()
        return {
            "ok": True,
            "summary": result.summary,
            "n_days": len(result.nav),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("nav.rebuild failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_code": "REBUILD_FAILED"}


def _run_batch_post_close(
    runner: JobRunner, services: dict[str, Any], store: Any, partition: str, timeout_s: int,
    trigger_id: int,
) -> None:
    """picker 调 batch_post_close: 需要 workflow + recorder (M11 step recorder)."""
    from akq_agents.orchestrator.jobs import batch_post_close
    from akq_agents.orchestrator.step_recorder import StepRecorder

    workflow = services.get("workflow")
    if workflow is None:
        logger.error("manual_trigger_picker: batch.post_close need workflow, missing")
        _finalize_trigger(store, trigger_id, status="failed")
        return
    repo = workflow.services.get("data_repository")
    recorder = None
    if repo is not None and hasattr(repo, "meta_db_path"):
        try:
            recorder = StepRecorder(
                repo.meta_db_path,
                parent_job_id="batch.post_close",
                parent_partition=partition,
            )
        except Exception:  # noqa: BLE001
            recorder = None

    ws_services = dict(workflow.services)
    ws_services["workflow"] = workflow  # _do 里 services["workflow"] 取
    if recorder is not None:
        ws_services["__recorder__"] = recorder

    fut = runner.submit(
        "batch.post_close", partition,
        lambda: batch_post_close._do(ws_services),
        timeout_s=timeout_s,
    )
    _attach_finalize_callback(fut, store, trigger_id, "batch.post_close", partition)


def _run_batch_deep_research(
    runner: JobRunner, services: dict[str, Any], store: Any, partition: str,
    payload: dict[str, Any], timeout_s: int, trigger_id: int,
) -> None:
    """picker 调 batch_deep_research: 透传 mode='fast'/'full'."""
    from akq_agents.orchestrator.jobs import batch_deep_research

    workflow = services.get("workflow")
    if workflow is None:
        logger.error("manual_trigger_picker: batch.deep_research need workflow, missing")
        _finalize_trigger(store, trigger_id, status="failed")
        return
    mode = payload.get("mode", "fast")
    fut = runner.submit(
        "batch.deep_research", partition,
        lambda: batch_deep_research._do(workflow.services, mode=mode),
        timeout_s=timeout_s,
    )
    _attach_finalize_callback(fut, store, trigger_id, "batch.deep_research", partition)


def _run_factor_discovery(
    runner: JobRunner, services: dict[str, Any], store: Any, partition: str,
    payload: dict[str, Any], timeout_s: int, trigger_id: int,
) -> None:
    """picker 调 factor.discovery: 透传 n_candidates."""
    from datetime import date as _date
    engine = services.get("discovery_engine")
    if engine is None:
        logger.error("manual_trigger_picker: factor.discovery need discovery_engine, missing")
        _finalize_trigger(store, trigger_id, status="failed")
        return
    n_candidates = int(payload.get("n_candidates", 20))

    def _do() -> dict[str, Any]:
        stats = engine.run_batch(n_candidates=n_candidates, as_of_date=_date.today())
        return stats.as_dict()

    fut = runner.submit("factor.discovery", partition, _do, timeout_s=timeout_s)
    _attach_finalize_callback(fut, store, trigger_id, "factor.discovery", partition)


def _run_factor_eviction(
    runner: JobRunner, services: dict[str, Any], store: Any, partition: str,
    payload: dict[str, Any], timeout_s: int, trigger_id: int,
) -> None:
    """picker 调 factor.eviction: 透传 dry_run, 加载 scheduler config."""
    from akq_agents.bootstrap import load_scheduler_config
    from akq_agents.orchestrator.jobs import factor_eviction

    workflow = services.get("workflow")
    if workflow is None:
        logger.error("manual_trigger_picker: factor.eviction need workflow, missing")
        _finalize_trigger(store, trigger_id, status="failed")
        return
    dry_run = bool(payload.get("dry_run", True))
    scheduler_cfg = load_scheduler_config()

    def _do() -> dict[str, Any]:
        return factor_eviction._do(workflow.services, scheduler_cfg, dry_run=dry_run)

    fut = runner.submit("factor.eviction", partition, _do, timeout_s=timeout_s)
    _attach_finalize_callback(fut, store, trigger_id, "factor.eviction", partition)
