"""``batch.post_close``：每个交易日 15:30 触发的盘后大任务。

P2 阶段：调用现有 ``QuantWorkflow.run_once``（DataAgent → FactorAgent → BacktestAgent →
ResearchAgent → PortfolioAgent → RiskAgent → AdvisorAgent → ReportAgent）。

P3 之后：替换为新的 P3 portfolio pipeline；本文件接口保持不变。

幂等性：``(job_id, date.today().isoformat())`` 由 :class:`JobRunner` 强制；
同一交易日重复触发，第二次直接 noop。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.job_runner import JobRunner

JOB_ID = "batch.post_close"


def register(
    scheduler: BackgroundScheduler,
    runner: JobRunner,
    cfg: SchedulerConfig,
    services: dict[str, Any],
) -> None:
    """注册到 APScheduler。配置 disabled 时直接 return。"""
    job_cfg = cfg.jobs.batch_post_close
    if not job_cfg.enabled:
        return

    def _run() -> None:
        partition = date.today().isoformat()
        runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)

    scheduler.add_job(
        _run,
        CronTrigger(hour=job_cfg.hour, minute=job_cfg.minute),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,  # 禁用 APScheduler 内置 misfire 补跑；missed 由 self_heal 处理
    )


def _do(services: dict[str, Any]) -> dict[str, Any]:
    """实际业务：调 workflow.run_once。

    services 至少需提供 ``workflow``：现有 QuantWorkflow 实例。
    """
    workflow = services["workflow"]
    # M11: 用 StepRecorder 把 agent 子步骤落到 job_steps 表
    recorder = _make_recorder(services)
    outputs = workflow.run_once(recorder=recorder) if recorder else workflow.run_once()
    # 汇总摘要（不要塞太大对象到 events.payload）
    advice = outputs.get("advisor-agent", {}) if isinstance(outputs, dict) else {}
    portfolio = outputs.get("portfolio-agent", {}) if isinstance(outputs, dict) else {}
    return {
        "agents": list(outputs.keys()) if isinstance(outputs, dict) else [],
        "advice_rendered_chars": len(advice.get("rendered", "")) if isinstance(advice, dict) else 0,
        "portfolio_n": portfolio.get("portfolio_size", 0) if isinstance(portfolio, dict) else 0,
    }


def _make_recorder(services: dict[str, Any]):
    """从 services 构造 StepRecorder。失败时返回 None（不阻塞 batch 主流程）。"""
    try:
        repo = services.get("data_repository")
        if repo is None:
            return None
        from akq_agents.orchestrator.step_recorder import StepRecorder

        meta_db = repo._base_dir / "meta.db"
        return StepRecorder(meta_db, parent_job_id=JOB_ID, parent_partition=date.today().isoformat())
    except Exception:
        return None


def run_once_now(runner: JobRunner, services: dict[str, Any], cfg: SchedulerConfig) -> None:
    """启动期 self_heal 补跑用的同步入口（不经 cron）。"""
    job_cfg = cfg.jobs.batch_post_close
    partition = date.today().isoformat()
    runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=job_cfg.timeout_s)
