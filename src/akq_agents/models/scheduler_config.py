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


class DataRefreshConfig(BaseModel):
    """今日 OHLCV 数据刷新 job（cron + 自适应重试）。

    交易日 16:00 首次尝试（数据源此时通常已就绪）；如果尚未就绪 / 失败，
    每 ``retry_interval_minutes`` 分钟重试一次，直到当天 quality_passed 为止，
    最晚到 ``stop_hour``:00 不再重试。
    """

    enabled: bool = True
    # 首次 cron 时间（仅交易日）
    first_try_hour: int = 16
    first_try_minute: int = 0
    # 重试节奏
    retry_interval_minutes: int = 30
    stop_hour: int = 22       # 当天 22:00 之后不再尝试
    timeout_s: int = 600       # 单次拉取超时 10 分钟（批量接口正常 ~15 秒，留 cushion）


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
    data_refresh: DataRefreshConfig = Field(default_factory=DataRefreshConfig)


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
