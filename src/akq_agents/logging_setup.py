"""统一日志配置。

问题背景:
之前 daemon / web 从不调用 ``logging.basicConfig``, 各模块 logger 无统一格式,
再靠 ``start.sh`` 的 ``> daemon.log 2>&1`` 收 stdout/stderr, 导致:
- 日志无时间戳 / 无级别 / 无来源模块, 前台只能靠正则猜等级;
- APScheduler / uvicorn 自己的裸输出混进来, 噪音大。

本模块提供 :func:`setup_logging`, 在 daemon / web 进程启动时调用一次,
让所有日志走同一个结构化格式:

    2026-07-02 14:52:01 | INFO  | akq_agents.orchestrator.scheduler | message

字段用 `` | `` 分隔, 便于 ``/api/ops/logs`` 逐行解析成结构化字段给前端渲染。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 字段分隔符 — 后端 Formatter 与前端/接口解析共用这一约定。
FIELD_SEP = " | "

# 统一格式: 时间(本地, 秒级) | 级别(左对齐5位) | logger 名 | 消息
_LOG_FORMAT = "%(asctime)s" + FIELD_SEP + "%(levelname)-5s" + FIELD_SEP + "%(name)s" + FIELD_SEP + "%(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 这些第三方 logger 太吵, 统一压到 WARNING(只保留真正的告警/错误)。
_NOISY_LOGGERS = {
    "apscheduler.scheduler": logging.WARNING,
    "apscheduler.executors.default": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
}

_CONFIGURED = False


def setup_logging(
    log_file: str | Path | None = None,
    *,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    console: bool = True,
) -> None:
    """配置根 logger。进程启动时调用一次, 幂等。

    参数:
        log_file: 日志文件路径。None 时只输出到 console(stdout)。
        level: 根级别, 默认 INFO。
        max_bytes / backup_count: RotatingFileHandler 轮转参数。
        console: 是否同时输出到 stdout(前台调试用)。
    """
    global _CONFIGURED

    root = logging.getLogger()
    root.setLevel(level)

    # 幂等: 重复调用时先清掉本模块加过的 handler, 避免重复行。
    for h in list(root.handlers):
        if getattr(h, "_akq_managed", False):
            root.removeHandler(h)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        fh.setLevel(level)
        fh._akq_managed = True  # type: ignore[attr-defined]
        root.addHandler(fh)

    if console:
        import sys

        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        ch.setLevel(level)
        ch._akq_managed = True  # type: ignore[attr-defined]
        root.addHandler(ch)

    # 压制吵闹的第三方 logger。
    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging configured (file=%s, level=%s)", log_file, logging.getLevelName(level)
    )


def attach_named_handler(
    logger_names: list[str],
    log_file: str | Path,
    *,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    propagate: bool = True,
) -> None:
    """给一组指定的 logger 追加一个独立的 RotatingFileHandler, 把它们的输出分流到自己的文件。

    典型场景: 回测/因子重算耗时长、体量大, 混在 daemon.log 里被 scheduler 噪音淹没,
    单独落一个 backtest.log 便于前台按源查看。

    参数:
        logger_names: 要分流的 logger 名列表 (例如 batch_deep_research 模块的 __name__)。
        log_file: 独立日志文件路径。
        propagate: 是否仍然向上冒泡到 root(daemon.log)。默认 True — 双写, 既在
                   独立文件里能看到, 也保留 daemon.log 里的时间线聚合视图。
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    fh = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setFormatter(formatter)
    fh.setLevel(level)
    fh._akq_managed = True  # type: ignore[attr-defined]
    # 用固定 tag 标识, 幂等调用时可以识别并只留一份。
    fh._akq_named_tag = str(log_path.resolve())  # type: ignore[attr-defined]

    for name in logger_names:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = propagate
        # 幂等: 已经挂过同一文件的 handler 就跳过, 避免重复行。
        already = any(
            getattr(h, "_akq_named_tag", None) == fh._akq_named_tag  # type: ignore[attr-defined]
            for h in lg.handlers
        )
        if not already:
            lg.addHandler(fh)


def parse_log_line(line: str) -> dict[str, str]:
    """把一行结构化日志解析成字段 dict。

    期望格式: ``ts | LEVEL | logger | message``。
    解析失败(如 traceback 续行 / 第三方裸输出)时, 返回 ``level=""`` 且
    ``msg`` 为整行原文, 让前端仍能显示。
    """
    parts = line.split(FIELD_SEP, 3)
    if len(parts) == 4:
        ts, level, name, msg = parts
        return {"ts": ts.strip(), "level": level.strip().upper(), "logger": name.strip(), "msg": msg}
    return {"ts": "", "level": "", "logger": "", "msg": line}
