"""ServiceContainer + FastAPI dependency 注入。

启动期一次性构造（@lru_cache(maxsize=1)）；前提是 uvicorn --workers 1。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass
class ServiceContainer:
    """汇总各阶段需要的服务实例。所有字段都允许为 None（测试覆盖部分场景时）。"""

    repo: Any = None
    sched_store: Any = None
    daemon_state_file: Any = None
    factor_registry: Any = None
    factor_evaluator: Any = None
    portfolio_store: Any = None
    llm_orchestrator: Any = None
    llm_store: Any = None
    llm_config: Any = None
    web_config: Any = None
    discovery_engine: Any = None
    proposal_store: Any = None
    workflow: Any = None
    paper_trading_store: Any = None
    # C5: web 进程也持有 JobRunner，让 trigger endpoint 能写 job_runs。
    # 与 daemon JobRunner 共用 sched_store + meta.db UNIQUE 约束保证不双写。
    job_runner: Any = None


_container_override: ServiceContainer | None = None


def set_container(container: ServiceContainer | None) -> None:
    """测试钩子：替换全局 container（用于 TestClient）。"""
    global _container_override
    _container_override = container
    get_services.cache_clear()


@lru_cache(maxsize=1)
def get_services() -> ServiceContainer:
    """启动时一次性构造 ServiceContainer。

    生产路径：from bootstrap import build_workflow + load_web_config + 装配 P2 daemon-state-file。
    测试路径：调用方先 set_container(...) 覆盖。
    """
    if _container_override is not None:
        return _container_override
    return _build_default()


def _build_default() -> ServiceContainer:
    """从现有 bootstrap 装配；缺什么就留 None（视图层会判 None 渲染 friendly 提示）。"""
    from concurrent.futures import ThreadPoolExecutor

    from akq_agents.bootstrap import build_workflow, load_web_config
    from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
    from akq_agents.orchestrator.job_runner import JobRunner

    workflow, _ = build_workflow()
    services = workflow.services
    repo = services.get("data_repository")
    daemon_state_file = None
    if repo is not None:
        daemon_state_file = DaemonStateFile(repo._base_dir / "daemon_state.json")

    # C5: web 进程造一个 JobRunner（与 daemon 共用 sched_store + meta.db UNIQUE 约束）。
    # web 用单线程池足够（trigger 是低频手动操作）。
    job_runner = None
    sched_store = services.get("scheduler_state_store")
    if sched_store is not None and repo is not None:
        job_runner = JobRunner(
            sched_store,
            is_trading_day=repo.is_trading_day,
            executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-job"),
        )

    return ServiceContainer(
        repo=repo,
        sched_store=sched_store,
        daemon_state_file=daemon_state_file,
        factor_registry=services.get("factor_registry"),
        factor_evaluator=services.get("factor_evaluator"),
        portfolio_store=services.get("portfolio_snapshot_store"),
        llm_orchestrator=services.get("llm_orchestrator"),
        llm_store=services.get("llm_store"),
        llm_config=services.get("llm_config"),
        web_config=load_web_config(),
        discovery_engine=services.get("discovery_engine"),
        proposal_store=services.get("factor_proposal_store"),
        workflow=workflow,
        paper_trading_store=services.get("paper_trading_store"),
        job_runner=job_runner,
    )
