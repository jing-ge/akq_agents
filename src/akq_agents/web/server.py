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
        # log_config=None: 不让 uvicorn 用自己的 logging 配置覆盖我们在 cmd_web_start
        # 里 setup_logging 装好的统一格式(带时间戳/级别/模块)。uvicorn 的 logger 会
        # 沿用 root handler, 从而落到同一个 web.log 且格式一致。
        log_config=None,
    )
