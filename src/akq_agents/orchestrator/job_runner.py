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

    def submit(
        self,
        job_id: str,
        partition: str,
        fn: Callable[[], dict[str, Any] | None],
        *,
        timeout_s: int,
    ) -> "concurrent.futures.Future[JobRunResult]":
        """M22: 提交后立即返回, 不阻塞 caller. 用于 web /api/jobs/{name}/trigger force_full
        这类用户手动触发但耗时长的场景 — web 端拿到 future 后立即返回 202 + job_id,
        用户去 /api/ops/job-runs 看进度。

        行为对齐 run() 的 4 步:
          1) 幂等检查 (ALREADY_OK → 立即返回 noop future)
          2) trading_day 护栏 (skip)
          3) 记 running
          4) 提交 fn 到 threadpool, future.result() 完成后由后台线程写 ok/failed/timeout

        区别: 不在 caller 线程等 result, 由 threadpool worker 完成后自己写 job_runs。
        即使 caller (web handler) 已经 return response, 后台仍会写完。
        """
        import concurrent.futures  # local import 避免污染模块顶部

        # 1) 幂等检查
        existing = self._store.get_job_run(job_id, partition)
        if existing is not None and existing.status == "ok":
            fut: concurrent.futures.Future = concurrent.futures.Future()
            fut.set_result(JobRunResult(job_id, partition, "noop", reason_code="ALREADY_OK"))
            return fut

        # 2) trading_day 护栏
        if job_id in self._required:
            today = date.today()
            try:
                if not self._is_trading_day(today):
                    self._record_skipped(job_id, partition, "NOT_TRADING_DAY")
                    fut2: concurrent.futures.Future = concurrent.futures.Future()
                    fut2.set_result(JobRunResult(job_id, partition, "skipped", reason_code="NOT_TRADING_DAY"))
                    return fut2
            except Exception as exc:  # noqa: BLE001
                self._record_failed(job_id, partition, "GUARD_ERROR", str(exc))
                fut3: concurrent.futures.Future = concurrent.futures.Future()
                fut3.set_result(JobRunResult(job_id, partition, "failed", reason_code="GUARD_ERROR"))
                return fut3

        # 3) 记 running
        started_at = datetime.now().isoformat()
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status="running",
            started_at=started_at,
        )

        # 4) 提交到 JobRunner 自带的 4-worker pool. 不同于 run() 的"嵌套 submit 再 result",
        # submit 路径必须直接在 worker 线程里跑 fn(), 否则 caller 释放 future 后 web handler
        # 立即返回, 但实际业务函数 _do(...) 还卡在"4 worker 池等自己嵌套的 future.result()"上.
        # 正确做法: 在 worker 线程直接调 fn, fn 内部自己管理 (asyncio.to_thread / ThreadPoolExecutor).
        def _wrapped() -> JobRunResult:
            start_mono = time.monotonic()
            try:
                payload = fn()
            except FuturesTimeout:
                return self._finish_run(job_id, partition, started_at, start_mono,
                                         "timeout", "TIMEOUT", payload=None)
            except Exception as exc:  # noqa: BLE001
                reason_code, kind_suffix = self._classify_exception(exc)
                return self._finish_run(job_id, partition, started_at, start_mono,
                                         "failed" if kind_suffix == "failed" else "skipped",
                                         reason_code, payload=None, exc=exc)
            else:
                return self._finish_run(job_id, partition, started_at, start_mono,
                                         "ok", None, payload=payload)

        return self._executor.submit(_wrapped)

    def _finish_run(
        self, job_id: str, partition: str, started_at: str, start_mono: float,
        status: str, reason_code: str | None,
        *, payload: Any = None, exc: Exception | None = None,
    ) -> JobRunResult:
        """submit() 路径的后处理: 写 job_runs + event, 返回 JobRunResult。"""
        duration_ms = int((time.monotonic() - start_mono) * 1000)
        finished_at = datetime.now().isoformat()
        self._store.upsert_job_run(
            job_id=job_id,
            partition=partition,
            status=status,
            reason_code=reason_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            payload=payload if isinstance(payload, dict) and status == "ok" else (
                {"error": str(exc)} if exc is not None else None
            ),
        )
        if status == "ok":
            self._store.write_event(
                level="info", kind=f"{job_id}.completed", source=job_id,
                payload={"partition": partition, "duration_ms": duration_ms,
                         **(payload if isinstance(payload, dict) else {})},
            )
        elif status == "timeout":
            self._store.write_event(
                level="warning", kind=f"{job_id}.timeout", source=job_id,
                payload={"partition": partition, "timeout_s": duration_ms // 1000},
            )
        else:
            self._store.write_event(
                level="error", kind=f"{job_id}.failed", source=job_id,
                payload={"partition": partition, "reason_code": reason_code,
                         "error": str(exc) if exc else None},
            )
        return JobRunResult(job_id, partition, status, reason_code=reason_code, duration_ms=duration_ms, payload=payload)

    def shutdown(self, *, wait: bool = False) -> None:
        """关闭内部 executor。"""
        self._executor.shutdown(wait=wait)
