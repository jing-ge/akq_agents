from __future__ import annotations

from pathlib import Path
from shutil import copyfile


class ReportExporter:
    def export_latest(self, markdown_path: str | None, html_path: str | None, export_dir: str) -> dict[str, str]:
        destination = Path(export_dir)
        destination.mkdir(parents=True, exist_ok=True)
        exported = {}
        if markdown_path:
            src = Path(markdown_path)
            dst = destination / src.name
            copyfile(src, dst)
            exported["markdown"] = str(dst)
        if html_path:
            src = Path(html_path)
            dst = destination / src.name
            copyfile(src, dst)
            exported["html"] = str(dst)
        return exported
