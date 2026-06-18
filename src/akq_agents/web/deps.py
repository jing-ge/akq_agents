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
    from akq_agents.bootstrap import build_workflow, load_web_config
    from akq_agents.orchestrator.daemon_state_file import DaemonStateFile

    workflow, _ = build_workflow()
    services = workflow.services
    repo = services.get("data_repository")
    daemon_state_file = None
    if repo is not None:
        daemon_state_file = DaemonStateFile(repo._base_dir / "daemon_state.json")
    return ServiceContainer(
        repo=repo,
        sched_store=services.get("scheduler_state_store"),
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
    )
