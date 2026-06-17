"""DaemonStateFile：``data/daemon_state.json`` 原子读写封装。

单进程状态用文件不入 db（spec §2 关键设计点 #7）：
- 启停时整文件原子替换（写临时文件 + rename）
- heartbeat 周期性更新 last_heartbeat
- is_alive 判定：last_heartbeat 距今 < max_age_s（默认 600s = 2 个 heartbeat 周期）
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class DaemonState:
    status: str  # starting | running | stopping | stopped
    pid: int
    started_at: str
    last_heartbeat: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DaemonStateFile:
    """data/daemon_state.json 的原子读写。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def write(self, state: DaemonState) -> None:
        """原子写：先写 tmp 文件，再 rename。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def read(self) -> DaemonState | None:
        if not self._path.exists():
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        return DaemonState(**data)

    def update_heartbeat(self) -> None:
        """只更新 last_heartbeat 字段；状态读旧值。"""
        state = self.read()
        if state is None:
            return
        state.last_heartbeat = datetime.now().isoformat()
        self.write(state)

    def is_alive(self, *, max_age_s: int = 600) -> bool:
        """是否活着：last_heartbeat 距今不超过 max_age_s。"""
        state = self.read()
        if state is None:
            return False
        if state.status in {"stopped"}:
            return False
        try:
            last = datetime.fromisoformat(state.last_heartbeat)
        except (TypeError, ValueError):
            return False
        return (datetime.now() - last).total_seconds() <= max_age_s
