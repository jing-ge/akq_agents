"""启动期硬校验 + localhost-only middleware。"""

from __future__ import annotations

import logging
import sys

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def assert_loopback_bind(host: str) -> None:
    """启动期校验 bind_host；非 loopback 直接 SystemExit。"""
    if host not in _LOOPBACK_HOSTS:
        msg = (
            f"[P5 web guard] bind_host 必须为 loopback (127.0.0.1 / localhost / ::1)，"
            f"实得 {host!r}。如需对外开放请使用反向代理 + TLS（P6 范畴）。"
        )
        print(msg, file=sys.stderr)
        raise SystemExit(2)


class LocalhostOnlyMiddleware(BaseHTTPMiddleware):
    """拒绝非 loopback 来源的请求（防止反向代理意外暴露）。"""

    async def dispatch(self, request: Request, call_next):
        client = request.client
        host = client.host if client else None
        if host not in _LOOPBACK_HOSTS:
            return JSONResponse(
                {"error": "non-local request rejected", "client": host},
                status_code=403,
            )
        return await call_next(request)
