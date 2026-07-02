"""P2 ``QuantDaemon``：单进程调度守护主入口。

把 :class:`SchedulerStateStore` / :class:`JobRunner` / :class:`DaemonStateFile` /
:func:`self_heal_on_boot` / :class:`GracefulShutdown` / 4 个 jobs 串起来。

启动序列：
1. ``self_heal_on_boot``：扫 running/interrupted → crashed；判定是否补跑 batch.post_close；
   触发 retention 清理。
2. ``mark_daemon_started`` 写 daemon_state.json + ``daemon.started`` event。
3. 注册 SIGTERM / SIGINT。
4. 注册 4 个 jobs（启用的）。
5. APScheduler 启动；主线程 wait 直到 GracefulShutdown.should_stop。
6. ``shutdown(wait_s)``：scheduler.shutdown → mark running 为 interrupted →
   写 daemon.stopped event → mark_daemon_stopped。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from akq_agents.models.scheduler_config import SchedulerConfig
from akq_agents.orchestrator.daemon_state_file import DaemonStateFile
from akq_agents.orchestrator.job_runner import JobRunner
from akq_agents.orchestrator.jobs import (
    alert_check,
    batch_deep_research,
    batch_post_close,
    data_refresh,
    factor_code_brainstorm,  # 重构: LLM 自由代码路径 (DSL 受限的 factor_brainstorm 已下线)
    factor_discovery,
    factor_eviction,
    factor_promote_shadows,
    health_heartbeat,
    manual_trigger_picker,
    retry_fetch_errors,
)
from akq_agents.orchestrator.signal_handler import (
    GracefulShutdown,
    mark_daemon_started,
    mark_daemon_stopped,
    self_heal_on_boot,
)
from akq_agents.orchestrator.state_store import SchedulerStateStore

logger = logging.getLogger(__name__)


class QuantDaemon:
    """单进程调度守护。"""

    def __init__(
        self,
        config: SchedulerConfig,
        services: dict[str, Any],
        *,
        state_store: SchedulerStateStore,
        daemon_state_file: DaemonStateFile,
        is_trading_day: Callable[[date], bool],
        version: str = "akq-agents",
        install_signals: bool = True,
    ) -> None:
        self._cfg = config
        self._services = services
        self._store = state_store
        self._daemon_state_file = daemon_state_file
        self._is_trading_day = is_trading_day
        self._version = version
        self._install_signals = install_signals

        self._scheduler: BackgroundScheduler | None = None
        self._runner: JobRunner | None = None
        self._shutdown = GracefulShutdown()

    # ------------------ lifecycle ------------------

    def start(self, *, block: bool = True) -> None:
        """启动 daemon。``block=True`` 则阻塞直到收到 SIGTERM/SIGINT。"""
        # 1) self_heal
        self._self_heal()

        # 2) mark_daemon_started
        import os

        mark_daemon_started(
            daemon_state_file=self._daemon_state_file,
            pid=os.getpid(),
            version=self._version,
        )
        self._store.write_event(
            level="info",
            kind="daemon.started",
            source="daemon",
            payload={"pid": os.getpid(), "version": self._version},
        )

        # 3) install signals
        if self._install_signals:
            self._shutdown.install()

        # 4) build scheduler + runner + register jobs
        scheduler = self._build_scheduler()
        runner = JobRunner(
            self._store,
            is_trading_day=self._is_trading_day,
        )
        self._scheduler = scheduler
        self._runner = runner
        self._register_jobs()
        scheduler.start()
        logger.info("QuantDaemon started; jobs registered: %s",
                    [j.id for j in scheduler.get_jobs()])

        if block:
            try:
                self._shutdown.wait()  # 阻塞直到 stop event
            finally:
                self.shutdown()

    def shutdown(self, *, grace_s: int | None = None) -> None:
        grace = grace_s if grace_s is not None else self._cfg.shutdown_grace_s
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            # 简单的 grace 等待：BackgroundScheduler.shutdown(wait=False) 已立即返回
            # 这里我们不能等线程池真正空闲（无法强杀），但把"正在跑的 job_runs”转 interrupted
            _ = grace
        # 在 mark interrupted **之前**抓取 running 的 job_ids，以便发对应 events
        running_runs = self._store.list_recent_runs(limit=50, status="running")
        running_partitions_by_job: dict[str, list[str]] = {}
        for r in running_runs:
            running_partitions_by_job.setdefault(r.job_id, []).append(r.partition)

        affected = self._store.mark_interrupted_running()
        for job_id, partitions in running_partitions_by_job.items():
            self._store.write_event(
                level="warning",
                kind=f"{job_id}.interrupted",
                source="daemon",
                payload={"affected": len(partitions), "partitions": partitions},
            )
        _ = affected
        if self._runner is not None:
            self._runner.shutdown(wait=False)
        self._store.write_event(
            level="info",
            kind="daemon.stopped",
            source="daemon",
            payload={},
        )
        mark_daemon_stopped(daemon_state_file=self._daemon_state_file)
        logger.info("QuantDaemon stopped")

    def request_stop(self) -> None:
        """外部主动请求停机（测试或主动健康检查失败时调用）。"""
        self._shutdown.request_stop()

    # ------------------ status (called by CLI `daemon status`) ------------------

    def status_payload(self) -> dict[str, Any]:
        state = self._daemon_state_file.read()
        is_alive = self._daemon_state_file.is_alive(
            max_age_s=self._cfg.jobs.health_heartbeat.interval_minutes * 60 * 2
        )
        return {
            "state": state.to_dict() if state else None,
            "is_alive": is_alive,
        }

    # ------------------ internal ------------------

    def _self_heal(self) -> dict[str, int]:
        """启动期 self_heal + retention 清理。"""
        backfill_post_close = self._make_backfill_post_close()
        stats = self_heal_on_boot(
            store=self._store,
            is_trading_day=self._is_trading_day,
            older_than_hours=6,
            post_close_hour=self._cfg.jobs.batch_post_close.hour,
            post_close_minute=self._cfg.jobs.batch_post_close.minute,
            backfill_post_close=backfill_post_close,
        )
        retention = self._store.cleanup(
            events_keep_days=self._cfg.retention.events_days,
            job_runs_keep_days=self._cfg.retention.job_runs_days,
        )
        logger.info("self_heal_on_boot: %s; retention: %s", stats, retention)
        return {**stats, **retention}

    def _make_backfill_post_close(self) -> Callable[[], None] | None:
        """生成补跑闭包：复用同一 JobRunner + services。

        注意：self_heal 在 scheduler 启动**之前**调用，此时 runner 还没建。
        所以这里创建一个临时 runner（专用于补跑这一次），不复用 self._runner。
        """
        if "workflow" not in self._services:
            return None

        def _backfill() -> None:
            tmp_runner = JobRunner(self._store, is_trading_day=self._is_trading_day)
            try:
                batch_post_close.run_once_now(tmp_runner, self._services, self._cfg)
            finally:
                tmp_runner.shutdown(wait=False)

        return _backfill

    def _build_scheduler(self) -> BackgroundScheduler:
        executors = {
            "default": APThreadPoolExecutor(max_workers=self._cfg.thread_pool_size)
        }
        return BackgroundScheduler(
            timezone=self._cfg.timezone,
            executors=executors,
        )

    def _register_jobs(self) -> None:
        assert self._scheduler is not None and self._runner is not None
        data_refresh.register(self._scheduler, self._runner, self._cfg, self._services)
        batch_post_close.register(self._scheduler, self._runner, self._cfg, self._services)
        batch_deep_research.register(self._scheduler, self._runner, self._cfg, self._services)
        retry_fetch_errors.register(self._scheduler, self._runner, self._cfg, self._services)
        factor_discovery.register(self._scheduler, self._runner, self._cfg, self._services)
        # DSL 受限的 factor_brainstorm 已下线 (LLM 撞库 100%, 靠 code_brainstorm 补位)
        # 重构: LLM 自由 Python 代码 brainstorm (不限定 DSL 空间, 走 sandbox 编译)
        factor_code_brainstorm.register(
            self._scheduler, self._runner, self._cfg, self._services,
        )
        factor_promote_shadows.register(self._scheduler, self._runner, self._cfg, self._services)
        factor_eviction.register(self._scheduler, self._runner, self._cfg, self._services)
        alert_check.register(self._scheduler, self._runner, self._cfg, self._services)
        health_heartbeat.register(self._scheduler, self._cfg, self._daemon_state_file)
        # M23: web → daemon 手动触发通道 picker. 必须最后注册 (max_instances=1 +
        # interval 5s 持续跑, 早注册也无所谓, 但放最后逻辑清晰).
        manual_trigger_picker.register(
            self._scheduler, self._runner, self._cfg, self._services,
        )
