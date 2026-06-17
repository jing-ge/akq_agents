"""``akq_agents.services.data`` —— P1 数据层子包。

对外只暴露 :class:`DataRepository` 与 :class:`UniverseManager`，
所有 AKShare 调用统一经 :class:`AKShareGateway`。

详见 ``docs/superpowers/specs/2026-06-17-p1-data-layer-design.md``。
"""

from akq_agents.services.data.exceptions import (
    DataError,
    DataNotReady,
    FetchError,
    QualityCheckFailed,
)
from akq_agents.services.data.schemas import (
    DataHealth,
    OHLCVBar,
    RefreshResult,
    UniverseSnapshot,
)

__all__ = [
    "DataError",
    "DataNotReady",
    "FetchError",
    "QualityCheckFailed",
    "DataHealth",
    "OHLCVBar",
    "RefreshResult",
    "UniverseSnapshot",
]
