"""QualityGate：每日 OHLCV 入库前的"准入闸门"。

对应 P1 spec §2 "关键设计点 - 质量门" 和 §3 流程 1：
- 行数 ≥ ``min_universe_size``
- 关键字段（``close, volume, amount``）非空率 > ``1 - max_null_rate``
- ``close`` 范围 [``min_close``, ``max_close``]

任一失败 → :class:`QualityCheckFailed` raise，且 ``checks`` 字段含每项 True/False。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pandas as pd

from akq_agents.models.data_config import QualityConfig
from akq_agents.services.data.exceptions import QualityCheckFailed


_REQUIRED_COLS = ("close", "volume", "amount")


@dataclass
class QualityGate:
    """对一个日级 OHLCV DataFrame 做三项检查并阻断脏数据落库。"""

    config: QualityConfig

    def check(self, df: pd.DataFrame) -> Dict[str, bool]:
        """跑全部检查；如任一失败 :class:`QualityCheckFailed`。

        :returns: 每项检查的 True/False 字典（全过时也返回，便于审计）
        """
        checks: Dict[str, bool] = {
            "row_count": self._check_row_count(df),
            "null_rate": self._check_null_rate(df),
            "close_range": self._check_close_range(df),
        }
        if not all(checks.values()):
            raise QualityCheckFailed(checks)
        return checks

    def _check_row_count(self, df: pd.DataFrame) -> bool:
        return len(df) >= self.config.min_universe_size

    def _check_null_rate(self, df: pd.DataFrame) -> bool:
        if df.empty:
            return False
        for col in _REQUIRED_COLS:
            if col not in df.columns:
                return False
            null_rate = float(df[col].isna().mean())
            if null_rate > self.config.max_null_rate:
                return False
        return True

    def _check_close_range(self, df: pd.DataFrame) -> bool:
        if df.empty or "close" not in df.columns:
            return False
        close = df["close"].dropna()
        if close.empty:
            return False
        return bool(close.min() >= self.config.min_close and close.max() <= self.config.max_close)
