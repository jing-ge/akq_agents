from __future__ import annotations

import importlib
import platform
import sys
from typing import Dict, List


class EnvironmentDoctor:
    def check(self) -> Dict[str, object]:
        packages = {}
        for name in ["akshare", "akquant", "pydantic", "pandas", "apscheduler", "yaml"]:
            packages[name] = self._package_status(name)
        return {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": packages,
            "recommendations": self._recommendations(packages),
        }

    def _package_status(self, name: str) -> Dict[str, str]:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "unknown")
            return {"status": "installed", "version": str(version)}
        except Exception:
            return {"status": "missing", "version": "-"}

    def _recommendations(self, packages: Dict[str, Dict[str, str]]) -> List[str]:
        notes = []
        if packages["akshare"]["status"] != "installed":
            notes.append("安装 akshare 以启用真实市场数据")
        if packages["akquant"]["status"] != "installed":
            notes.append("安装 akquant 以启用真实回测")
        if not notes:
            notes.append("真实链路基础依赖已就绪，可测试 strict_real_services=true")
        return notes
