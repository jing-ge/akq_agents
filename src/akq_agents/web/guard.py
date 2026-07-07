"""启动期硬校验 + localhost-only middleware。"""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# 需要 CSRF 防护的写方法 (GET/HEAD/OPTIONS 是安全方法, 不校验)
_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
    """拒绝非 loopback 来源的请求（防止反向代理意外暴露）。

    注意：``request.client`` 偶尔为 None（HTTP/2 prefetch、Safari preconnect、
    或一些浏览器特殊连接路径下不带 client info）。由于 server 已绑定 loopback，
    任何能到达这里的请求物理上都来自本机，因此 None 视为放行（否则会出现
    偶发的 /research 403，前端表现为"卡住"）。
    """

    async def dispatch(self, request: Request, call_next):
        client = request.client
        host = client.host if client else None
        # host=None 放行：bind=loopback 时 None 也是本机来源
        if host is not None and host not in _LOOPBACK_HOSTS:
            return JSONResponse(
                {"error": "non-local request rejected", "client": host},
                status_code=403,
            )
        return await call_next(request)


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """对写方法 (POST/PUT/PATCH/DELETE) 做 Origin/Referer 校验, 防 CSRF。

    即使 server 只绑 loopback, 本地浏览器打开的恶意网页仍可对 127.0.0.1
    发起跨站写请求 (浏览器允许跨源请求发往 localhost)。这里校验:
    - 若请求带 Origin 或 Referer 头, 其 host 必须是 loopback, 否则 403;
    - 若两者都无 (非浏览器客户端: CLI / curl / 内部脚本), 放行 —
      这类请求不受浏览器同源策略约束, CSRF 不适用, 且拦了会破坏现有 API 调用。

    安全方法 (GET/HEAD/OPTIONS) 一律放行。
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                host = urlparse(origin).hostname
                if host not in _LOOPBACK_HOSTS:
                    return JSONResponse(
                        {"error": "cross-origin write rejected", "origin": origin},
                        status_code=403,
                    )
        return await call_next(request)
