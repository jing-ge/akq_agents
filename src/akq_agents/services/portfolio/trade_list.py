"""TradeListAgent + holdings 表（P0-1）。

把"权重浮点数"翻译成"我下一步该下的具体股数 + 金额 + 原因"。

核心数据流：
1. holdings 表：当前真实持仓 {symbol: shares, avg_cost}
   - 由用户手动校准（"我昨天没买进 600519，按 0 股记"）
   - daemon 不自动改 holdings（关键：保证 holdings 永远反映"真实下单后的世界"）

2. 每天 daemon 跑完 portfolio 后，TradeListAgent 计算：
   - target_shares = round(weight × capital / today_close, 100)
   - delta = target_shares - current_shares
   - delta > 0 → BUY
   - delta < 0 → SELL
   - delta == 0 → HOLD
   - 用 risk_filter / industry_map 加注释（"权重 +1.2%" / "新进 top 50" / "被风控剔除"）

3. trade_list_cohorts 表存每天的清单 snapshot：用户可以历史回看
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_HOLDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
  symbol TEXT PRIMARY KEY,
  shares REAL NOT NULL,
  avg_cost REAL,
  updated_at TEXT NOT NULL,
  note TEXT
);
"""

_TRADE_LIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_list_cohorts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cohort_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,            -- BUY / SELL / HOLD
  current_shares REAL NOT NULL,
  target_shares REAL NOT NULL,
  delta_shares REAL NOT NULL,
  target_weight REAL,
  current_price REAL,
  delta_amount REAL,               -- 估计交易金额
  reason TEXT,                     -- 文字理由
  industry TEXT,
  composite_score REAL,
  executed INTEGER DEFAULT 0,      -- 0 未执行 / 1 已确认
  created_at TEXT NOT NULL,
  UNIQUE(cohort_date, symbol)
);
"""

_INDEX_TL = "CREATE INDEX IF NOT EXISTS idx_trade_list_cohort_date ON trade_list_cohorts(cohort_date);"


@dataclass
class TradeListConfig:
    """假定本金 + 整手设置。"""
    assumed_capital: float = 100_000.0
    lot_size: int = 100               # A 股一手 = 100 股
    min_trade_amount: float = 200.0   # 小于这个金额的"边角料"不下单


@dataclass
class TradeItem:
    symbol: str
    action: str                       # BUY / SELL / HOLD
    current_shares: float
    target_shares: float
    delta_shares: float
    target_weight: float | None
    current_price: float | None
    delta_amount: float
    reason: str
    industry: str | None
    composite_score: float | None


class HoldingsStore:
    """用户的真实持仓（手动校准）。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_HOLDINGS_SCHEMA)
            conn.commit()

    def list_all(self) -> list[dict]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT symbol, shares, avg_cost, updated_at, note FROM holdings ORDER BY shares DESC"
            ).fetchall()
        return [
            {"symbol": r[0], "shares": r[1], "avg_cost": r[2], "updated_at": r[3], "note": r[4]}
            for r in rows
        ]

    def upsert(self, symbol: str, shares: float, avg_cost: float | None = None, note: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with open_meta_db(self._db) as conn:
            if shares <= 0:
                # 0 股直接删除
                conn.execute("DELETE FROM holdings WHERE symbol = ?", (str(symbol),))
            else:
                conn.execute(
                    """
                    INSERT INTO holdings (symbol, shares, avg_cost, updated_at, note)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                      shares=excluded.shares,
                      avg_cost=COALESCE(excluded.avg_cost, holdings.avg_cost),
                      updated_at=excluded.updated_at,
                      note=COALESCE(excluded.note, holdings.note)
                    """,
                    (str(symbol), float(shares), avg_cost, now, note),
                )
            conn.commit()

    def get_shares(self, symbol: str) -> float:
        with open_meta_db(self._db) as conn:
            row = conn.execute("SELECT shares FROM holdings WHERE symbol = ?", (str(symbol),)).fetchone()
        return float(row[0]) if row else 0.0

    def as_dict(self) -> dict[str, float]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute("SELECT symbol, shares FROM holdings").fetchall()
        return {str(s): float(sh) for s, sh in rows}


class TradeListStore:
    """trade_list_cohorts 表读写。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_TRADE_LIST_SCHEMA)
            conn.execute(_INDEX_TL)
            conn.commit()

    def upsert_cohort(self, cohort_date: date, items: list[TradeItem]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        new_symbols = {it.symbol for it in items}
        rows = [
            (
                cohort_date.isoformat(),
                it.symbol,
                it.action,
                it.current_shares,
                it.target_shares,
                it.delta_shares,
                it.target_weight,
                it.current_price,
                it.delta_amount,
                it.reason,
                it.industry,
                it.composite_score,
                0,
                now,
            )
            for it in items
        ]
        with open_meta_db(self._db) as conn:
            # 先删掉这一天里不在新清单里的 symbol（持仓删除后清空旧 SELL 等）
            if new_symbols:
                placeholders = ",".join("?" for _ in new_symbols)
                conn.execute(
                    f"DELETE FROM trade_list_cohorts WHERE cohort_date = ? AND symbol NOT IN ({placeholders})",
                    (cohort_date.isoformat(), *new_symbols),
                )
            else:
                # 完全空：清掉这一天所有的
                conn.execute(
                    "DELETE FROM trade_list_cohorts WHERE cohort_date = ?",
                    (cohort_date.isoformat(),),
                )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO trade_list_cohorts
                      (cohort_date, symbol, action, current_shares, target_shares,
                       delta_shares, target_weight, current_price, delta_amount,
                       reason, industry, composite_score, executed, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cohort_date, symbol) DO UPDATE SET
                      action=excluded.action,
                      current_shares=excluded.current_shares,
                      target_shares=excluded.target_shares,
                      delta_shares=excluded.delta_shares,
                      target_weight=excluded.target_weight,
                      current_price=excluded.current_price,
                      delta_amount=excluded.delta_amount,
                      reason=excluded.reason,
                      industry=excluded.industry,
                      composite_score=excluded.composite_score
                    """,
                    rows,
                )
            conn.commit()
        return len(rows)

    def list_cohort(self, cohort_date: date) -> list[dict]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT symbol, action, current_shares, target_shares, delta_shares,
                       target_weight, current_price, delta_amount, reason,
                       industry, composite_score, executed
                FROM trade_list_cohorts
                WHERE cohort_date = ?
                ORDER BY
                  CASE action WHEN 'BUY' THEN 0 WHEN 'SELL' THEN 1 ELSE 2 END,
                  ABS(delta_amount) DESC
                """,
                (cohort_date.isoformat(),),
            ).fetchall()
        return [
            {
                "symbol": r[0], "action": r[1],
                "current_shares": r[2], "target_shares": r[3], "delta_shares": r[4],
                "target_weight": r[5], "current_price": r[6], "delta_amount": r[7],
                "reason": r[8], "industry": r[9], "composite_score": r[10],
                "executed": bool(r[11]),
            }
            for r in rows
        ]

    def list_dates(self, limit: int = 30) -> list[str]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT DISTINCT cohort_date FROM trade_list_cohorts ORDER BY cohort_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    def mark_executed(
        self,
        cohort_date: date,
        symbol: str,
        *,
        holdings_store: HoldingsStore | None = None,
    ) -> None:
        """标记某条已执行；可选同时把 target_shares 同步到 holdings。"""
        target_shares: float | None = None
        with open_meta_db(self._db) as conn:
            if holdings_store is not None:
                row = conn.execute(
                    "SELECT target_shares FROM trade_list_cohorts WHERE cohort_date=? AND symbol=?",
                    (cohort_date.isoformat(), str(symbol)),
                ).fetchone()
                if row is not None:
                    target_shares = float(row[0])
            conn.execute(
                "UPDATE trade_list_cohorts SET executed=1 WHERE cohort_date=? AND symbol=?",
                (cohort_date.isoformat(), str(symbol)),
            )
            conn.commit()
        if holdings_store is not None and target_shares is not None:
            holdings_store.upsert(
                str(symbol),
                shares=target_shares,
                note=f"executed cohort {cohort_date.isoformat()}",
            )


def generate_trade_list(
    *,
    cohort_date: date,
    target_weights: dict[str, float],
    current_close: dict[str, float],
    holdings: dict[str, float],
    composite_scores: dict[str, float] | None = None,
    industry_map: dict[str, str] | None = None,
    yesterday_weights: dict[str, float] | None = None,
    cfg: TradeListConfig | None = None,
) -> list[TradeItem]:
    """计算今日交易清单。

    Args:
        target_weights: 今日 portfolio 推荐权重 {symbol: w}
        current_close: 今日 close 价格 {symbol: price}
        holdings: 当前真实持仓 {symbol: shares}
        composite_scores: 可选，因子综合评分
        industry_map: 可选，{symbol: industry_name}
        yesterday_weights: 可选，昨日权重（用于生成原因文案）
    """
    cfg = cfg or TradeListConfig()
    composite_scores = composite_scores or {}
    industry_map = industry_map or {}
    yesterday_weights = yesterday_weights or {}

    # 所有相关 symbol = 今日推荐 ∪ 当前持仓
    all_symbols = set(target_weights) | set(holdings)
    items: list[TradeItem] = []

    for sym in all_symbols:
        target_w = float(target_weights.get(sym, 0.0))
        current_shares = float(holdings.get(sym, 0.0))
        price = current_close.get(sym)

        if price is None or price <= 0:
            # 没价格的特殊处理：保持现状 HOLD
            items.append(TradeItem(
                symbol=sym, action="HOLD",
                current_shares=current_shares, target_shares=current_shares,
                delta_shares=0.0,
                target_weight=target_w if target_w > 0 else None,
                current_price=None, delta_amount=0.0,
                reason="价格缺失，保持现状",
                industry=industry_map.get(sym),
                composite_score=composite_scores.get(sym),
            ))
            continue

        # 目标股数：按整手取整（A 股最小 100 股）
        target_amount = target_w * cfg.assumed_capital
        target_shares_raw = target_amount / price
        target_shares = round(target_shares_raw / cfg.lot_size) * cfg.lot_size
        delta_shares = target_shares - current_shares
        delta_amount = delta_shares * price

        # 太小的交易跳过（手续费都不够）
        if 0 < abs(delta_amount) < cfg.min_trade_amount:
            target_shares = current_shares
            delta_shares = 0
            delta_amount = 0

        # 决策 + 文字理由
        yest_w = float(yesterday_weights.get(sym, 0.0))
        if delta_shares > 0:
            action = "BUY"
            if current_shares == 0:
                reason = f"新进推荐（权重 {target_w*100:.2f}%）"
            elif yest_w > 0:
                reason = f"权重 {yest_w*100:.2f}% → {target_w*100:.2f}%，加仓"
            else:
                reason = f"加仓到 {target_w*100:.2f}%"
        elif delta_shares < 0:
            action = "SELL"
            if target_shares == 0:
                if target_w == 0:
                    reason = "已不在推荐组合（行业/风控/评分被剔除）"
                else:
                    reason = f"目标权重 {target_w*100:.2f}% 不足一手"
            else:
                reason = f"权重 {yest_w*100:.2f}% → {target_w*100:.2f}%，减仓"
        else:
            action = "HOLD"
            if target_w > 0:
                reason = f"权重 {target_w*100:.2f}%，保持"
            else:
                reason = "无变动"

        items.append(TradeItem(
            symbol=sym,
            action=action,
            current_shares=current_shares,
            target_shares=float(target_shares),
            delta_shares=float(delta_shares),
            target_weight=target_w if target_w > 0 else None,
            current_price=price,
            delta_amount=float(delta_amount),
            reason=reason,
            industry=industry_map.get(sym),
            composite_score=composite_scores.get(sym),
        ))

    return items
