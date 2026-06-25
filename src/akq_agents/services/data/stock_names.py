"""股票代码 → 中文简称映射 (meta.db.stock_names)。

UI 显示用：交易清单里只有 6 位代码（600519），用户看不出是哪家。

数据来源：``AKShareGateway.fetch_spot()`` 返回 ``[symbol, name, listing_date]``。
- 全量约 5500 行，几 KB，启动时 lazy load 到内存即可。
- 名称变化频率低（极个别 ST/重组），按需 backfill 而不是每日刷。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_names (
  symbol TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class StockNameStore:
    """`stock_names` 表的读写接口。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def load_all(self) -> dict[str, str]:
        """返回 {symbol: name}。空表返回 {}。"""
        try:
            with open_meta_db(self._db) as conn:
                rows = conn.execute("SELECT symbol, name FROM stock_names").fetchall()
            return {str(s): str(n) for s, n in rows}
        except Exception as exc:
            logger.warning("StockNameStore.load_all failed: %s", exc)
            return {}

    def upsert_many(self, name_map: dict[str, str]) -> int:
        """批量写入 {symbol: name}。返回写入行数。"""
        if not name_map:
            return 0
        ts = datetime.now().isoformat(timespec="seconds")
        rows = [(str(s), str(n), ts) for s, n in name_map.items() if n]
        with open_meta_db(self._db) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_names (symbol, name, updated_at) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        return len(rows)
