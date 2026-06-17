from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from typing import Dict, Optional


class ReportExporter:
    def export_latest(self, markdown_path: Optional[str], html_path: Optional[str], export_dir: str) -> Dict[str, str]:
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
