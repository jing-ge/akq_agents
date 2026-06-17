"""P3 因子库对外出口。"""

from akq_agents.services.factors.base import Factor, FactorRegistry
from akq_agents.services.factors.engine import FactorEngine
from akq_agents.services.factors.liquidity import amount_20
from akq_agents.services.factors.momentum import momentum_5, momentum_20, momentum_60
from akq_agents.services.factors.reversal import reversal_5
from akq_agents.services.factors.size import log_amount_20
from akq_agents.services.factors.volatility import volatility_20


def build_default_registry() -> FactorRegistry:
    """P3a 默认因子注册表：6 个价格类因子。"""
    reg = FactorRegistry()
    reg.register(momentum_5())
    reg.register(momentum_20())
    reg.register(momentum_60())
    reg.register(reversal_5())
    reg.register(volatility_20())
    reg.register(amount_20())
    reg.register(log_amount_20())
    return reg


__all__ = [
    "Factor",
    "FactorEngine",
    "FactorRegistry",
    "build_default_registry",
]
