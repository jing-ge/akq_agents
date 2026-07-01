"""JobRunner：P2 调度任务统一执行入口。

职责：
- **幂等记账**：``(job_id, partition)`` 已 status='ok' → noop（不重复执行）
- **trading_day 护栏**：可选；通过白名单（``trading_day_required_jobs``）决定是否启用
- **超时记账**：用 ``ThreadPoolExecutor.submit`` + ``Future.result(timeout)``，
  超时后**不强杀**（Python 线程限制），但状态会让后续不再重复触发同一 partition
- **异常→events**：捕获所有异常，写 ``job_runs`` + ``events``，永不抛出给上层

不知道业务，只做"幂等 + 超时 + 记账 + 护栏"。
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from akq_agents.orchestrator.state_store import SchedulerStateStore

logger = logging.getLogger(__name__)


@dataclass
class JobRunResult:
    job_id: str
    partition: str
    status: Literal["ok", "failed", "skipped", "timeout", "noop"]
    reason_code: str | None = None
    duration_ms: int | None = None
    payload: dict[str, Any] | None = None


# trading_day 护栏白名单：这些 job_id **需要**今天是交易日才执行；
# 其它 job（retry / heartbeat）不受护栏约束。
DEFAULT_TRADING_DAY_REQUIRED: frozenset[str] = frozenset(
    {
        "batch.post_close",
        "data.refresh_daily",
        # M19 review: factor.brainstorm 不再依赖今日交易数据 — LLM 提议靠 prompt
        # 里的历史 metrics + 拒绝率统计 + 已上线因子列表, 周末跑出来的提议跟
        # 工作日跑出来的没差异。之前周末 cron skipped 导致用户周一看不到新提议。
        # "factor.brainstorm",
        # batch.deep_research 不限交易日 — 用历史数据滚动评估 factor_metrics,
        # 周末也要跑, 否则 shadow OOS 计数拖慢 (少 2 天/周 = 晋升周期拉长 28%)。
        # yaml 注释明确写"不限交易日 (节假日也跑)", 之前白名单包含它属于配置/代码矛盾。
        # "batch.deep_research",
        "factor.discovery",
        "factor.promote_shadows",
    }
)


class JobRunner:
    """统一执行入口；job 函数应在 fn 中实现纯业务，不关心记账与护栏。"""

    def __init__(
        self,
        state_store: SchedulerStateStore,
        is_trading_day: Callable[[date], bool],
        *,
        trading_day_required_jobs: frozenset[str] = DEFAULT_TRADING_DAY_REQUIRED,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._store = state_store
        self._is_trading_day = is_trading_day
        self._required = trading_day_required_jobs
        # 默认 4-worker 池；可外部注入以便测试
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="job")

    def run(
        self,
        job_id: str,
        partition: str,
        fn: Callable[[], dict[str, Any] | None],
        *,
        timeout_s: int,
    ) -> JobRunResult:
        """执行一个 job；返回 JobRunResult。永不抛出。"""
        # 1) 幂等检查
        existing = self._store.get_job_run(job_id, partition)
        if existing is not None and existing.status == "ok":
            return JobRunResult(job_id, partition, "noop", reason_code="ALREADY_OK")

        # 2) trading_day 护栏
        if job_id in self._required:
            today = date.today()
            try:
                if not self._is_trading_day(today):
                    self._record_skipped(job_id, partition, "NOT_TRADING_DAY")
                    return JobRunResult(job_id, partition, "skipped", reason_code="NOT_TRADING_DAY")
            except Exception as exc:  # noqa: BLE001
                # 护栏自身异常 → 视为不通过；记 failed
                self._record_failed(job_id, partition, "GUARD_ERROR", str(exc))
                return JobRunResult(job_id, partition, "failed", reason_code="GUARD_ERROR")

        # 3) 记录 running
        started_at = datetime.now().isoformat()
        start_mono = time.monotonic()
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status="running",
            started_at=started_at,
        )

        # 4) 执行（带超时记账）
        future = self._executor.submit(fn)
        try:
            payload = future.result(timeout=timeout_s)
        except FuturesTimeout:
            duration_ms = int((time.monotonic() - start_mono) * 1000)
            self._store.upsert_job_run(
                job_id=job_id,
                partition=partition,
                status="timeout",
                reason_code="TIMEOUT",
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
                duration_ms=duration_ms,
            )
            self._store.write_event(
                level="warning",
                kind=f"{job_id}.timeout",
                source=job_id,
                payload={"partition": partition, "timeout_s": timeout_s},
            )
            return JobRunResult(job_id, partition, "timeout", reason_code="TIMEOUT", duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001 — 必须吞掉所有业务异常
            duration_ms = int((time.monotonic() - start_mono) * 1000)
            tb = traceback.format_exc()
            reason_code, kind_suffix = self._classify_exception(exc)
            self._store.upsert_job_run(
                job_id=job_id,
                partition=partition,
                status="failed" if kind_suffix == "failed" else "skipped",
                reason_code=reason_code,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
                duration_ms=duration_ms,
                payload={"error": str(exc)},
            )
            self._store.write_event(
                level="error" if kind_suffix == "failed" else "info",
                kind=f"{job_id}.{kind_suffix}",
                source=job_id,
                payload={
                    "partition": partition,
                    "reason_code": reason_code,
                    "error": str(exc),
                    "traceback": tb,
                },
            )
            logger.exception("job %s partition %s failed: %s", job_id, partition, exc)
            return JobRunResult(
                job_id,
                partition,
                "failed" if kind_suffix == "failed" else "skipped",
                reason_code=reason_code,
                duration_ms=duration_ms,
            )

        # 5) 成功路径
        duration_ms = int((time.monotonic() - start_mono) * 1000)
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status="ok",
            started_at=started_at,
            finished_at=datetime.now().isoformat(),
            duration_ms=duration_ms,
            payload=payload if isinstance(payload, dict) else None,
        )
        self._store.write_event(
            level="info",
            kind=f"{job_id}.completed",
            source=job_id,
            payload={
                "partition": partition,
                "duration_ms": duration_ms,
                **(payload if isinstance(payload, dict) else {}),
            },
        )
        return JobRunResult(job_id, partition, "ok", duration_ms=duration_ms, payload=payload)

    # ----------------- internal helpers -----------------

    def _record_skipped(self, job_id: str, partition: str, reason_code: str) -> None:
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status="skipped",
            reason_code=reason_code,
            started_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(),
            duration_ms=0,
        )
        self._store.write_event(
            level="info",
            kind=f"{job_id}.skipped",
            source=job_id,
            payload={"partition": partition, "reason_code": reason_code},
        )

    def _record_failed(self, job_id: str, partition: str, reason_code: str, message: str) -> None:
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status="failed",
            reason_code=reason_code,
            started_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(),
            duration_ms=0,
            payload={"error": message},
        )
        self._store.write_event(
            level="error",
            kind=f"{job_id}.failed",
            source=job_id,
            payload={"partition": partition, "reason_code": reason_code, "error": message},
        )

    def _classify_exception(self, exc: Exception) -> tuple[str, str]:
        """将异常映射为 (reason_code, event_kind_suffix)。

        - DataNotReady → ('DATA_NOT_READY', 'skipped')  P1 异常
        - 其它任何异常 → ('UNKNOWN', 'failed')
        """
        cls_name = type(exc).__name__
        if cls_name == "DataNotReady":  # 用名字比较，避免循环导入
            return "DATA_NOT_READY", "skipped"
        return "UNKNOWN", "failed"

    def shutdown(self, *, wait: bool = False) -> None:
        """关闭内部 executor。"""
        self._executor.shutdown(wait=wait)
