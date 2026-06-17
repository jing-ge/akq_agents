"""数据层标准异常。

按 P1 设计文档（docs/superpowers/specs/2026-06-17-p1-data-layer-design.md §4 异常分类）：
- ``FetchError``：拉取阶段所有可恢复/不可恢复错误的统一封装，带 reason_code
- ``DataNotReady``：缓存未命中、Agent 应跳过本轮
- ``QualityCheckFailed``：QualityGate 拒入缓存

所有数据层抛出的异常均继承自 :class:`DataError`，便于上游统一捕获。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

ReasonCode = Literal["RATE_LIMITED", "SCHEMA_DRIFT", "NETWORK", "UNKNOWN"]


class DataError(Exception):
    """数据层根异常。"""


class FetchError(DataError):
    """从 AKShare 拉取数据失败。

    :param reason_code: 错误归类，便于重试策略与告警分级
    :param symbol: 失败的标的代码（若适用）
    :param message: 原始错误信息，纯文本
    """

    def __init__(
        self,
        reason_code: ReasonCode,
        message: str = "",
        symbol: str | None = None,
    ) -> None:
        self.reason_code: ReasonCode = reason_code
        self.symbol = symbol
        self.message = message
        super().__init__(f"[{reason_code}] symbol={symbol} {message}")


class DataNotReady(DataError):
    """缓存中缺少所需数据；Agent 应跳过本轮。"""

    def __init__(self, missing: dict[str, list[date]]) -> None:
        self.missing = missing
        sample = next(iter(missing.items())) if missing else None
        super().__init__(f"data not ready, missing {len(missing)} symbols; sample={sample}")


class QualityCheckFailed(DataError):
    """QualityGate 校验失败，拒绝写入缓存。"""

    def __init__(self, checks: dict[str, bool]) -> None:
        self.checks = checks
        failed = [name for name, ok in checks.items() if not ok]
        super().__init__(f"quality check failed: {failed}")
