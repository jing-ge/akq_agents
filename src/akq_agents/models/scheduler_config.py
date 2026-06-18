"""P2 调度守护配置 pydantic 模型，加载自 ``config/scheduler.yaml``。

由 :class:`SchedulerConfig` 统一承载 4 个 job 配置 + retention 策略。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BatchJobConfig(BaseModel):
    """盘后 batch 类 job 的统一配置（cron 触发）。"""

    enabled: bool = True
    timeout_s: int = 5400  # 90min，含 P3 + P4
    hour: int = 15
    minute: int = 30
    # batch.deep_research 使用 day_of_week='sun'；batch.post_close 不用
    day_of_week: str | None = None


class IntervalJobConfig(BaseModel):
    """interval 类 job 配置（retry / heartbeat）。"""

    enabled: bool = True
    interval_minutes: int = 5
    timeout_s: int = 60


class FactorDiscoveryConfig(BaseModel):
    """因子自动发现 job（interval 触发，含每次抽样数量）。"""

    enabled: bool = True
    interval_minutes: int = 60
    timeout_s: int = 900
    n_candidates_per_run: int = 20


class SchedulerJobsConfig(BaseModel):
    batch_post_close: BatchJobConfig = Field(default_factory=BatchJobConfig)
    batch_deep_research: BatchJobConfig = Field(
        default_factory=lambda: BatchJobConfig(
            enabled=False, hour=22, minute=0, day_of_week="sun", timeout_s=5400
        )
    )
    retry_fetch_errors: IntervalJobConfig = Field(
        default_factory=lambda: IntervalJobConfig(interval_minutes=5, timeout_s=60)
    )
    health_heartbeat: IntervalJobConfig = Field(
        default_factory=lambda: IntervalJobConfig(interval_minutes=5, timeout_s=5)
    )
    factor_discovery: FactorDiscoveryConfig = Field(default_factory=FactorDiscoveryConfig)


class RetentionConfig(BaseModel):
    events_days: int = 30
    job_runs_days: int = 90


class SchedulerConfig(BaseModel):
    """``config/scheduler.yaml`` 顶层 ``scheduler:`` 节对应的根配置。"""

    timezone: str = "Asia/Shanghai"
    thread_pool_size: int = 4
    shutdown_grace_s: int = 30
    jobs: SchedulerJobsConfig = Field(default_factory=SchedulerJobsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SchedulerConfig:
        with open(path, encoding="utf-8") as handle:
            payload: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls.model_validate(payload.get("scheduler", {}))
