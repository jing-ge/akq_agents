"""执行轨迹记录器（M11）：把 batch.post_close / factor.discovery 等任务内部
的子步骤详情写到 ``job_steps`` 表，供 UI 展开查看。

设计：
- ``job_steps``：parent_job_id + parent_partition + step_seq + step_name +
  started_at + finished_at + status + payload_json
- ``StepRecorder`` context manager: ``with recorder.step("data-agent"): ...``
- payload 可以塞 任意 JSON：输入摘要、输出摘要、关键指标。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_job_id TEXT NOT NULL,
  parent_partition TEXT NOT NULL,
  step_seq INTEGER NOT NULL,
  step_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  duration_ms INTEGER,
  payload_json TEXT,
  error TEXT
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_job_steps_parent
  ON job_steps(parent_job_id, parent_partition, step_seq);
"""


class StepRecorder:
    """把一个父任务的子步骤逐条写到 job_steps 表。"""

    def __init__(self, meta_db_path: Path, parent_job_id: str, parent_partition: str) -> None:
        self._db = Path(meta_db_path)
        self._parent_job_id = parent_job_id
        self._parent_partition = parent_partition
        self._seq = 0
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()

    @contextmanager
    def step(self, name: str, payload_in: dict[str, Any] | None = None):
        """记录一个步骤：with recorder.step('data-agent', {...}): ... -> payload_out

        用法：
            with recorder.step('data-agent') as ctx:
                result = run()
                ctx.set_payload({'rows': len(result), ...})
        """
        self._seq += 1
        seq = self._seq
        started = datetime.now()
        t0 = time.monotonic()
        rec = _StepContext(payload_in or {})
        row_id = self._insert_start(name, seq, started)
        try:
            yield rec
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._update_done(row_id, "ok", rec.payload, None, duration_ms)
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            err = f"{type(exc).__name__}: {exc!s}"[:500]
            self._update_done(row_id, "failed", rec.payload, err, duration_ms)
            raise

    def _insert_start(self, name: str, seq: int, started: datetime) -> int:
        with open_meta_db(self._db) as conn:
            cur = conn.execute(
                """
                INSERT INTO job_steps
                  (parent_job_id, parent_partition, step_seq, step_name,
                   started_at, status)
                VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (self._parent_job_id, self._parent_partition, seq, name,
                 started.isoformat(timespec="seconds")),
            )
            conn.commit()
            return cur.lastrowid

    def _update_done(self, row_id: int, status: str, payload: dict | None,
                     error: str | None, duration_ms: int) -> None:
        finished = datetime.now().isoformat(timespec="seconds")
        with open_meta_db(self._db) as conn:
            conn.execute(
                """
                UPDATE job_steps
                SET finished_at=?, status=?, payload_json=?, error=?, duration_ms=?
                WHERE id=?
                """,
                (finished, status,
                 json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
                 error, duration_ms, row_id),
            )
            conn.commit()


class _StepContext:
    """让 with 块内部能更新 payload。"""

    def __init__(self, initial: dict[str, Any]) -> None:
        self.payload = dict(initial)

    def set_payload(self, p: dict[str, Any]) -> None:
        self.payload.update(p)


class StepReader:
    """读取 job_steps 表。"""

    def __init__(self, meta_db_path: Path) -> None:
        self._db = Path(meta_db_path)
        # 确保表存在（避免 SELECT 时报 'no such table'）
        with open_meta_db(self._db) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()

    def list_steps(self, parent_job_id: str, parent_partition: str) -> list[dict]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, step_seq, step_name, started_at, finished_at,
                       status, duration_ms, payload_json, error
                FROM job_steps
                WHERE parent_job_id = ? AND parent_partition = ?
                ORDER BY step_seq ASC
                """,
                (parent_job_id, parent_partition),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "seq": r[1], "name": r[2],
                "started_at": r[3], "finished_at": r[4],
                "status": r[5], "duration_ms": r[6],
                "payload": json.loads(r[7]) if r[7] else None,
                "error": r[8],
            })
        return out
