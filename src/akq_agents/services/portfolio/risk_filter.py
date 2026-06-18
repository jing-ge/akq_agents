"""M7-B：组合层的硬风控过滤。

universe 阶段（PortfolioAgent._run_p3）调用，剔除：
1. 上市天数不足（默认 60 交易日）
2. 停牌（当日 volume == 0 或 amount == 0）
3. 流动性不足（过去 20 日日均成交额 < 5000 万）
4. 价格极端（当日 close < 1 或 > 1000 元）

返回：白名单 + 黑名单原因映射。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RiskFilterConfig:
    min_listing_days: int = 60
    min_avg_amount: float = 5e7    # 5000 万日均成交额
    min_price: float = 1.0
    max_price: float = 1000.0
    amount_window: int = 20


@dataclass
class RiskFilterResult:
    kept: list[str]
    excluded: dict[str, str]  # symbol → reason

    @property
    def excluded_count_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.excluded.values():
            out[r] = out.get(r, 0) + 1
        return out


class RiskFilter:
    """OHLCV-based 运行时硬风控过滤。"""

    def __init__(self, cfg: RiskFilterConfig | None = None) -> None:
        self._cfg = cfg or RiskFilterConfig()

    def apply(
        self,
        candidate_symbols: list[str],
        ohlcv: pd.DataFrame,
        as_of_date: date,
    ) -> RiskFilterResult:
        """对 candidate_symbols 应用过滤。

        ohlcv: long-format，至少含 [date, symbol, close, volume, amount] 列；
            建议是过去 ``amount_window + 10`` 交易日。
        """
        cfg = self._cfg
        excluded: dict[str, str] = {}
        kept: list[str] = []

        if ohlcv.empty:
            return RiskFilterResult(kept=[], excluded={s: "NO_OHLCV" for s in candidate_symbols})

        # 按 symbol 分组，预聚合
        # 注意：ohlcv 可能不包含所有 candidate_symbols
        grouped = ohlcv.groupby("symbol", sort=False)
        groups_keys = set(ohlcv["symbol"].unique())

        for sym in candidate_symbols:
            if sym not in groups_keys:
                excluded[sym] = "NO_OHLCV"
                continue
            sub = grouped.get_group(sym).sort_values("date")
            # 1) 上市天数（用 OHLCV 历史长度近似）
            if len(sub) < cfg.min_listing_days:
                excluded[sym] = f"NEW_LISTING_LT_{cfg.min_listing_days}D"
                continue
            # 2) 当日（last row）停牌：volume 或 amount == 0
            last = sub.iloc[-1]
            last_volume = float(last.get("volume", 0) or 0)
            last_amount = float(last.get("amount", 0) or 0)
            if last_volume <= 0 or last_amount <= 0:
                excluded[sym] = "SUSPENDED"
                continue
            # 3) 价格极端
            last_close = float(last.get("close", 0) or 0)
            if last_close <= 0 or last_close < cfg.min_price:
                excluded[sym] = f"PRICE_LT_{cfg.min_price}"
                continue
            if last_close > cfg.max_price:
                excluded[sym] = f"PRICE_GT_{cfg.max_price}"
                continue
            # 4) 流动性：过去 20 日日均成交额
            tail = sub.tail(cfg.amount_window)
            avg_amount = float(tail["amount"].fillna(0).mean()) if "amount" in tail.columns else 0.0
            if avg_amount < cfg.min_avg_amount:
                excluded[sym] = f"LOW_LIQUIDITY_LT_{cfg.min_avg_amount:.0f}"
                continue
            kept.append(sym)

        logger.info(
            "risk_filter: kept=%d excluded=%d reasons=%s",
            len(kept), len(excluded),
            {r: list(excluded.values()).count(r) for r in set(excluded.values())},
        )
        return RiskFilterResult(kept=kept, excluded=excluded)
