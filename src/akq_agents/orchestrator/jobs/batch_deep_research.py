"""``batch.deep_research``：每周日 22:00 跑因子有效性滚动评估（P3a 实现）。

对 ``factor_registry.list_all()`` 每个因子：
- 拉过去 max(lookback+60+5) 天 OHLCV
- 按日跑因子计算 → factor_history (index=date, columns=symbol)
- 下一日收益 forward_returns = close.pct_change().shift(-1)
- 调 ``FactorEvaluator.evaluate`` 计算 IC/IR/t-stat 并写 factor_metrics 表

注意：本任务跑得慢（O(N_factors × N_days × N_symbols)）但不影响盘后日报，所以
没有强 timeout 压力；spec §3 流程 2 已明确说明。
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
    factor_evaluator。缺任一关键依赖时即便 enabled=True 也跳过注册。"""
    job_cfg = cfg.jobs.batch_deep_research
    if not job_cfg.enabled:
        return
    if not _has_required_services(services):
        logger.info("batch.deep_research enabled but P3 services missing; skip registration")
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
    return all(k in services for k in {"data_repository", "factor_registry", "factor_evaluator"})


def _do(services: dict[str, Any]) -> dict[str, Any]:
    """实际业务：对每个 factor 做 rolling IC 评估并写表。"""
    import pandas as pd

    repo = services["data_repository"]
    registry = services["factor_registry"]
    evaluator = services["factor_evaluator"]

    today = date.today()
    universe = repo.get_universe(today)
    full_symbols = universe.symbols

    # 限制 universe 减少计算量：取 top 500 by 流动性，与 PortfolioAgent 一致
    from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

    # 拉一段 OHLCV 用于评估
    max_lookback = max((f.lookback_days for f in registry.list_all()), default=80)
    window = evaluator._window if hasattr(evaluator, "_window") else 60
    history_days = max_lookback + window + 10
    start = today - timedelta(days=history_days * 2)
    # 用宽容读：缺哪天就缺哪天（PortfolioAgent 同款）
    ohlcv = repo.get_ohlcv_loose(full_symbols, start, today)
    if ohlcv.empty:
        return {"factors_evaluated": 0, "window": window, "reason": "no_data"}
    portfolio_universe = build_portfolio_universe(
        full_universe_symbols=full_symbols, ohlcv=ohlcv, top_n=500, window=20
    )
    sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(portfolio_universe))]
    # close pivot 一次性
    close = sub_ohlcv.pivot_table(
        index="date", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    forward_returns = close.pct_change().shift(-1)

    metrics_written = 0
    for factor in registry.list_all():
        # 按日跑因子计算（rolling）。简化做法：每个 as_of_date 用 sub_ohlcv[date <= d] 子集
        factor_history_rows: dict = {}
        for d in close.index:
            d_date = d.date() if hasattr(d, "date") else d
            sub = sub_ohlcv[sub_ohlcv["date"] <= d_date]
            if len(sub) < factor.lookback_days:
                continue
            s = factor.compute(sub)
            factor_history_rows[d] = s
        if not factor_history_rows:
            continue
        factor_history = pd.DataFrame(factor_history_rows).T
        # 对齐 forward_returns
        common_idx = factor_history.index.intersection(forward_returns.index)
        if len(common_idx) < window:
            continue
        evaluator.evaluate(
            factor=factor,
            factor_history=factor_history.loc[common_idx],
            forward_returns=forward_returns.loc[common_idx],
            as_of_date=today,
        )
        metrics_written += 1

    return {"factors_evaluated": metrics_written, "window": window}
