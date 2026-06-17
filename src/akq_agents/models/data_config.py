"""数据层配置 pydantic 模型，加载自 ``config/data.yaml``。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class UniverseFilterConfig(BaseModel):
    market: str = "cn"
    include_st: bool = False
    include_new: bool = False
    min_listing_days: int = 180
    min_price: float = 1.0
    max_price: float = 1000.0


class AkshareGatewayConfig(BaseModel):
    qps: float = 5.0
    max_retries: int = 3
    timeout_s: int = 30
    backoff_base_s: float = 0.5


class CacheConfig(BaseModel):
    ohlcv_lookback_days: int = 250
    financials_lookback_quarters: int = 8


class QualityConfig(BaseModel):
    min_universe_size: int = 4000
    max_null_rate: float = 0.01
    min_close: float = 0.5
    max_close: float = 2000.0


class DataConfig(BaseModel):
    """数据层根配置，对应 ``config/data.yaml`` 顶层 ``data:`` 节。"""

    base_dir: str = "./data"
    universe: UniverseFilterConfig = Field(default_factory=UniverseFilterConfig)
    akshare: AkshareGatewayConfig = Field(default_factory=AkshareGatewayConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> DataConfig:
        with open(path, encoding="utf-8") as handle:
            payload: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls.model_validate(payload.get("data", {}))

    def resolve_base_dir(self, project_root: Path) -> Path:
        """把 base_dir 相对路径解析到项目根目录下。"""
        base = Path(self.base_dir)
        if base.is_absolute():
            return base
        return (project_root / base).resolve()
