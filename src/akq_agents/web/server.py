"""P5 Web 服务器启动入口。

CLI 用法：``akq-agents web start``。
强制 ``workers=1``，与 ``@lru_cache`` ServiceContainer 单例假设一致。
"""

from __future__ import annotations

import logging

from akq_agents.web.guard import assert_loopback_bind

logger = logging.getLogger(__name__)


def start(host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动 uvicorn 服务器（前台阻塞，Ctrl+C 退出）。"""
    assert_loopback_bind(host)
    import uvicorn

    logger.info("starting AKQ Agents Console at http://%s:%d (workers=1)", host, port)
    uvicorn.run(
        "akq_agents.web.app:app",
        host=host,
        port=port,
        workers=1,  # 硬编码，与 lru_cache 单例假设一致
        log_level="info",
    )
