"""行业映射读取（M9-C）。

读 meta.db.industry_map → {symbol: industry_code} or {symbol: industry_name}。
"""

from __future__ import annotations

import logging
from pathlib import Path

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


class IndustryMapStore:
    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)

    def load(self) -> dict[str, str]:
        """返回 {symbol: industry_code}。空表则返回 {}。"""
        try:
            with open_meta_db(self._db) as conn:
                rows = conn.execute(
                    "SELECT symbol, industry_code FROM industry_map"
                ).fetchall()
            return {str(s): str(c) for s, c in rows}
        except Exception as exc:
            logger.warning("IndustryMapStore.load failed: %s", exc)
            return {}

    def load_names(self) -> dict[str, str]:
        """返回 {symbol: industry_name}（用于 portfolio 显示）。"""
        try:
            with open_meta_db(self._db) as conn:
                rows = conn.execute(
                    "SELECT symbol, industry_name FROM industry_map"
                ).fetchall()
            return {str(s): str(n) for s, n in rows}
        except Exception as exc:
            logger.warning("IndustryMapStore.load_names failed: %s", exc)
            return {}
