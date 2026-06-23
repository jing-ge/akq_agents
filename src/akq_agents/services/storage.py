from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    def save(self, state: dict[str, Any]) -> None:
        with open(self.path, "w", encoding="utf-8") as file:
            yaml.safe_dump(state, file, allow_unicode=True, sort_keys=False)
