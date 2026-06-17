from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional


class NotificationService:
    def build_message(self, title: str, markdown_path: Optional[str], html_path: Optional[str]) -> str:
        parts = [f"标题: {title}"]
        if markdown_path:
            parts.append(f"Markdown 报告: {markdown_path}")
        if html_path:
            parts.append(f"HTML 报告: {html_path}")
        parts.append("提示: 可在此处接入企业微信、飞书、邮件或 Telegram 发送逻辑")
        return "\n".join(parts)

    def notify_stub(self, title: str, markdown_path: Optional[str], html_path: Optional[str], output_file: str) -> Dict[str, str]:
        message = self.build_message(title, markdown_path, html_path)
        path = Path(output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(message + "\n", encoding="utf-8")
        return {"notification_preview": str(path)}
