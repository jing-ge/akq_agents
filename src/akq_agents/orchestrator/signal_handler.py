"""SignalHandler + self_heal_on_boot：daemon 生命周期辅助逻辑。

- :func:`self_heal_on_boot`：daemon 启动时扫 ``running/interrupted`` 标 ``crashed``，
  按白名单（``batch.post_close`` / ``batch.deep_research``）判断是否补跑。
- :class:`GracefulShutdown`：捕获 SIGTERM/SIGINT，触发 :class:`QuantDaemon.shutdown`。
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import date, datetime

from akq_agents.orchestrator.daemon_state_file import DaemonState, DaemonStateFile
from akq_agents.orchestrator.state_store import SchedulerStateStore

logger = logging.getLogger(__name__)


SELF_HEAL_WHITELIST = frozenset({"batch.post_close", "batch.deep_research"})


def self_heal_on_boot(
    *,
    store: SchedulerStateStore,
    is_trading_day: Callable[[date], bool],
    older_than_hours: int = 6,
    post_close_hour: int = 15,
    post_close_minute: int = 30,
    backfill_post_close: Callable[[], None] | None = None,
) -> dict[str, int]:
    """启动期一次性扫描 + 补跑判定 + retention 清理（由 daemon.start 调用）。

    返回统计 dict：{crashed, backfilled, ...}
    """
    stats = {"crashed_marked": 0, "backfilled": 0, "triggers_reclaimed": 0}

    # 1) 把 running/interrupted 老旧记录标 crashed
    candidates = store.list_runs_to_self_heal(older_than_hours=older_than_hours)
    for run in candidates:
        store.mark_crashed(run.id)
        # 写一条 events 表示这一行被自愈
        kind = f"{run.job_id}.crashed"
        store.write_event(
            level="error",
            kind=kind,
            source=run.job_id,
            payload={"partition": run.partition, "previous_status": run.status},
        )
        stats["crashed_marked"] += 1

    # 1b) 回收僵尸 pending_triggers: 上一个 daemon 的 picker claim 后进程已死,
    #     claimed 记录会永久占用 job 名额, 导致手动再触发被 409 拒。daemon 重启
    #     即意味着旧 picker 不存在, 一律回收为 failed。
    try:
        reclaimed = store.reclaim_stale_pending_triggers()
        if reclaimed:
            logger.warning("self_heal: reclaimed %d stale claimed pending_triggers", reclaimed)
            store.write_event(
                level="warn",
                kind="daemon.triggers_reclaimed",
                source="self_heal",
                payload={"count": reclaimed},
            )
        stats["triggers_reclaimed"] = reclaimed
    except Exception as exc:  # noqa: BLE001
        logger.exception("self_heal: reclaim stale triggers failed: %s", exc)

    # 2) 补跑判定（仅 batch.post_close）
    today = date.today()
    today_iso = today.isoformat()
    try:
        is_today_trading = is_trading_day(today)
    except Exception:  # noqa: BLE001
        is_today_trading = False

    if backfill_post_close is not None and is_today_trading:
        existing = store.get_job_run("batch.post_close", today_iso)
        already_ok = existing is not None and existing.status == "ok"
        now = datetime.now()
        post_close_passed = (now.hour, now.minute) >= (post_close_hour, post_close_minute)
        if not already_ok and post_close_passed:
            logger.info(
                "self_heal: backfilling batch.post_close for %s (post_close window passed, not yet ok)",
                today_iso,
            )
            try:
                backfill_post_close()
                stats["backfilled"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("self_heal backfill failed: %s", exc)

    return stats


def mark_daemon_started(*, daemon_state_file: DaemonStateFile, pid: int, version: str) -> None:
    """daemon 启动后写 daemon_state.json 并写 events。"""
    now = datetime.now().isoformat()
    daemon_state_file.write(
        DaemonState(
            status="running",
            pid=pid,
            started_at=now,
            last_heartbeat=now,
            version=version,
        )
    )


def mark_daemon_stopped(*, daemon_state_file: DaemonStateFile) -> None:
    state = daemon_state_file.read()
    if state is None:
        return
    state.status = "stopped"
    state.last_heartbeat = datetime.now().isoformat()
    daemon_state_file.write(state)


class GracefulShutdown:
    """注册 SIGTERM / SIGINT 处理器，给 daemon 提供 ``request_stop()`` 接口。

    设计：信号到达 → set 内部 Event；daemon 主循环周期性检查 event 决定退出。
    复杂的"等运行中任务 ≤ grace_s 再 hard stop"由 :class:`QuantDaemon.shutdown` 负责。
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._installed = False

    def install(self) -> None:
        """安装信号处理器。仅主线程可调用。"""
        if self._installed:
            return
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self._installed = True

    def _on_signal(self, signum: int, _frame: object) -> None:
        logger.info("graceful shutdown signal received: %s", signum)
        self._stop_event.set()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """阻塞等待 stop 信号；返回 True 表示触发了 stop。"""
        return self._stop_event.wait(timeout=timeout)

    def request_stop(self) -> None:
        """主动触发 stop（用于测试或 daemon 自检失败时主动退出）。"""
        self._stop_event.set()
