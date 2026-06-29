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
    # I4: default 必须晚于 data_refresh.first_try_hour=16，否则 batch 跑时当日数据
    # 还没刷出来，portfolio-agent 会因 DataNotReady 退化。yaml 文件丢失时也安全。
    hour: int = 16
    minute: int = 30
    # batch.deep_research 使用 day_of_week='sun'；batch.post_close 不用
    day_of_week: str | None = None


class IntervalJobConfig(BaseModel):
    """interval 类 job 配置（retry / heartbeat）。"""

    enabled: bool = True
    interval_minutes: int = 5
    timeout_s: int = 60


class FactorDiscoveryConfig(BaseModel):
    """因子自动发现 job（interval 触发，含每次抽样数量）。

    走 trading_day 白名单：非交易日 / 节假日 / 周末自动跳过（避免周末 48h 跑 960 个候选灌库）。
    """

    enabled: bool = True
    interval_minutes: int = 120  # 每 2 小时一次（之前 60 太密，配合白名单后这个值更稳）
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


class FactorBrainstormConfig(BaseModel):
    """LLM 因子构建方向建议 job（每日 cron 20:00）。

    走 trading_day 白名单。每次产出 n_suggestions 条 status='llm_suggested' 记录，
    等待人工 /research 页审核。
    """

    enabled: bool = True
    hour: int = 20
    minute: int = 0
    timeout_s: int = 120
    n_suggestions: int = 20


class FactorPromoteShadowsConfig(BaseModel):
    """Shadow 因子 OOS 评估 / 晋升 / 降级 job（每日 cron 17:30）。

    M19: 之前 _promote_shadows 耦合在 factor.discovery 主流程, ohlcv empty 时
    discovery 直接 return 不调用它, 导致 shadow OOS 计数永远 NULL. 拆出来独立 daily 跑,
    与 discovery 解耦。
    """

    enabled: bool = True
    hour: int = 17
    minute: int = 30
    timeout_s: int = 1800


class FactorEvictionConfig(BaseModel):
    """因子池淘汰 job (M19 weekly 周一 03:00)。

    用量化 factor_score = 0.5*|EWMA_30d_IR| + 0.3*|t_stat|/3 + 0.2*status_weight 排序,
    低分 + 超出 max_pool_size 的物理删除. 不给 builtin/accepted 绝对保护 (统一量化指标)。
    """

    enabled: bool = True
    day_of_week: str = "mon"
    hour: int = 3
    minute: int = 0
    timeout_s: int = 300
    # 淘汰参数
    max_pool_size: int = 300              # 总盘硬上限
    min_score: float = 0.05               # 软淘汰阈值
    new_factor_grace_days: int = 14       # 新因子保护期 (仅对 shadow/llm_suggested/accepted 生效)


class AlerterConfig(BaseModel):
    """M17 alerter job：定期巡检几项关键指标，触发条件就写 events.alert.* + macOS notify。"""

    enabled: bool = True
    interval_minutes: int = 30
    timeout_s: int = 30
    # 阈值
    nav_max_abs_daily_return: float = 0.15  # 单日 |daily_return| > 此值告警 (C3 那种伪净值的兜底)
    refresh_max_consecutive_failed: int = 2  # data.refresh_daily 连续 N 次 failed
    factor_decay_min_abs_ir: float = 0.05    # accepted 因子最近 5 天平均 |IR| < 此值告警
    factor_metrics_max_stale_days: int = 3   # M19: factor_metrics 表 N 天无新写入则告警


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
    factor_brainstorm: FactorBrainstormConfig = Field(default_factory=FactorBrainstormConfig)
    factor_promote_shadows: FactorPromoteShadowsConfig = Field(default_factory=FactorPromoteShadowsConfig)
    factor_eviction: FactorEvictionConfig = Field(default_factory=FactorEvictionConfig)
    data_refresh: DataRefreshConfig = Field(default_factory=DataRefreshConfig)
    alerter: AlerterConfig = Field(default_factory=AlerterConfig)


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
