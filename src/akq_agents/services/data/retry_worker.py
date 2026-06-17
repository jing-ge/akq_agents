"""失败重试 worker。

定期扫描 meta.db.fetch_errors 中 unresolved 记录，按 reason_code 决定是否
再次调用 gateway 重拉；成功 → 标 resolved=1，失败 → retry_count+=1。
连续失败超阈值自动放弃并打 give_up 标记。

P1 阶段：本 worker 不自带调度，由 CLI 单次触发或外部 scheduler 拉起。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

from akq_agents.services.data.akshare_gateway import AKShareGateway
from akq_agents.services.data.exceptions import FetchError
from akq_agents.services.data.repository import DataRepository

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    max_retries: int = 3
    batch_size: int = 100
    skip_reason_codes: tuple[str, ...] = ("UNKNOWN",)


class RetryWorker:
    def __init__(
        self,
        repository: DataRepository,
        gateway: AKShareGateway,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._policy = policy or RetryPolicy()

    def run_once(self) -> dict[str, int]:
        """跑一轮扫描+重试，返回统计信息。"""
        stats = {"scanned": 0, "resolved": 0, "still_failing": 0, "given_up": 0}
        with sqlite3.connect(self._repository._meta_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = self._list_pending(conn)
            stats["scanned"] = len(rows)
            for row in rows:
                succeeded, message = self._retry_single(row)
                if succeeded:
                    self._mark_resolved(conn, int(row["id"]))
                    stats["resolved"] += 1
                    continue

                next_retry_count = int(row["retry_count"]) + 1
                if next_retry_count >= self._policy.max_retries:
                    self._mark_given_up(conn, int(row["id"]), message)
                    stats["given_up"] += 1
                else:
                    self._mark_failed(conn, int(row["id"]), message)
                    stats["still_failing"] += 1
            conn.commit()
        return stats

    def _list_pending(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """列出本轮待处理的错误记录。"""
        placeholders = ", ".join("?" for _ in self._policy.skip_reason_codes)
        sql = (
            "SELECT * FROM fetch_errors WHERE resolved = 0 "
            "AND retry_count < ? "
            f"AND reason_code NOT IN ({placeholders}) "
            "ORDER BY id ASC LIMIT ?"
        )
        params: tuple[object, ...] = (
            self._policy.max_retries,
            *self._policy.skip_reason_codes,
            self._policy.batch_size,
        )
        return list(conn.execute(sql, params).fetchall())

    def _retry_single(self, row: sqlite3.Row) -> tuple[bool, str]:
        """根据 endpoint 调对应 gateway 方法重拉。"""
        endpoint = str(row["endpoint"])
        symbol = row["symbol"]
        current_message = str(row["message"] or "")
        try:
            if endpoint == "spot":
                self._gateway.fetch_spot()
            elif endpoint == "ohlcv":
                target_date = date.fromisoformat(str(row["target_date"]))
                self._gateway.fetch_ohlcv(str(symbol), target_date, target_date)
            elif endpoint == "st":
                self._gateway.fetch_st_list()
            elif endpoint == "individual":
                self._gateway.fetch_individual_info(str(symbol))
            elif endpoint == "calendar":
                self._gateway.fetch_trading_dates()
            else:
                logger.warning("unknown retry endpoint: %s", endpoint)
                return False, current_message
        except FetchError as exc:
            return False, exc.message
        return True, current_message

    def _mark_resolved(self, conn: sqlite3.Connection, row_id: int) -> None:
        conn.execute("UPDATE fetch_errors SET resolved = 1 WHERE id = ?", (row_id,))

    def _mark_failed(self, conn: sqlite3.Connection, row_id: int, message: str) -> None:
        conn.execute(
            "UPDATE fetch_errors SET retry_count = retry_count + 1, message = ? WHERE id = ?",
            (message, row_id),
        )

    def _mark_given_up(self, conn: sqlite3.Connection, row_id: int, message: str) -> None:
        """retry_count 达到上限 → resolved=2 表示放弃。"""
        conn.execute(
            "UPDATE fetch_errors SET retry_count = retry_count + 1, resolved = 2, message = ? WHERE id = ?",
            (message, row_id),
        )
