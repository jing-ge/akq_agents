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


def _do(services: dict[str, Any]) -> dict[str, Any]:
    """实际业务：对每个 factor 做 rolling IC 评估并写表。"""
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

    metrics_written = 0
    failures = 0
    for factor in evaluation_targets:
        try:
            # 按日跑因子计算 (rolling). 每个 as_of_date 用 sub_ohlcv[date <= d] 子集
            factor_history_rows: dict = {}
            for d in close.index:
                d_date = d.date() if hasattr(d, "date") else d
                sub = sub_ohlcv[sub_ohlcv["date"] <= d_date]
                if len(sub) < factor.lookback_days:
                    continue
                try:
                    s = factor.compute(sub)
                except Exception:
                    continue
                if s is None or s.empty:
                    continue
                factor_history_rows[d] = s
            if not factor_history_rows:
                failures += 1
                continue
            factor_history = pd.DataFrame(factor_history_rows).T
            # 对齐 forward_returns
            common_idx = factor_history.index.intersection(forward_returns.index)
            if len(common_idx) < window:
                failures += 1
                continue
            evaluator.evaluate(
                factor=factor,
                factor_history=factor_history.loc[common_idx],
                forward_returns=forward_returns.loc[common_idx],
                as_of_date=today,
            )
            metrics_written += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch.deep_research: factor %s evaluate failed: %s", factor.name, exc)
            failures += 1

    return {
        "factors_evaluated": metrics_written,
        "factors_failed": failures,
        "n_targets": len(evaluation_targets),
        "window": window,
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
