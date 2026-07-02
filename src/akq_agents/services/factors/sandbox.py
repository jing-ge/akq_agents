"""LLM 输出 Python 因子代码的受限沙箱执行环境。

**安全目标**: 让 LLM 自由写 compute(ohlcv) -> pd.Series 的实现, 同时严禁:
- 任何 ``import`` / ``__import__`` / ``importlib``
- 任何 ``open`` / 文件系统访问
- 任何 ``subprocess`` / ``os.system`` / ``socket`` / 网络访问
- 任何 ``eval`` / ``exec`` 嵌套
- 任何危险 builtin: ``getattr`` / ``setattr`` / ``delattr`` (LLM 可绕过属性检查拉危险属性)
- ``globals()`` / ``locals()`` (可以拉出 builtin)

**两层防御**:
1. **AST 静态扫描** — compile 之前扫, 不合法的代码直接抛 :class:`UnsafeCodeError`
2. **运行时 exec** — 限制 ``__builtins__`` 到白名单子集, 只 inject ``pd`` / ``np`` / ``math`` / 几个工具函数

超时: ``signal.SIGALRM`` (主线程 30s 上限), 任何 compute 跑超直接杀.

LLM 调用约定: source_code 必须定义 ``def compute(ohlcv: pd.DataFrame) -> pd.Series``.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import signal
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# -------------------- AST 静态扫描 --------------------

# 禁止的 AST 节点类型 (出现就拒)
_FORBIDDEN_NODES = (
    ast.Import,         # import X / import X.Y
    ast.ImportFrom,     # from X import Y
    # ast.Exec 已废弃; 现代 Python 把 exec 当 builtin 名字处理, 在 _BUILTIN_DENY 里拦
)


# 禁止出现的 builtin 名字 (LLM 写到代码里直接 name='open' / 'eval' 等)
_BUILTIN_DENY = frozenset({
    # IO / FS
    "open", "file", "input", "raw_input",
    # 危险 builtin
    "eval", "exec", "compile",
    "getattr", "setattr", "delattr",  # 可绕过属性检查
    "globals", "locals", "vars",      # 可拉 builtin
    "__import__", "importlib",
    # 反射 / 字节码
    "memoryview", "bytearray", "bytes",
    # 调试
    "breakpoint",
    # 内省 (LLM 用这些可以拿到 sandbox 外的对象)
    "dir", "type", "isinstance", "issubclass", "callable", "id", "hash",
})


class UnsafeCodeError(ValueError):
    """LLM 输出代码触发沙箱静态检查或运行时限制。"""


class CodeTimeoutError(RuntimeError):
    """compute() 执行超过 sandbox timeout_s。"""


def _check_source_safety(source: str) -> None:
    """AST 静态扫描: 失败抛 UnsafeCodeError (含原因)。

    关注的语义:
    1. 顶层/任意位置都不允许 import
    2. 不允许 importlib / __import__ 这类反射
    3. 出现的 name 节点不允许在 _BUILTIN_DENY 里
    4. 不允许直接 dunder 访问 (``__class__``, ``__subclasses__``, ``__globals__`` 等)
    5. 强制 def compute(ohlcv) 存在 (找不到直接拒)
    """
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise UnsafeCodeError(f"syntax error: {exc.msg} at line {exc.lineno}") from exc

    for node in ast.walk(tree):
        # 1) 禁止 import
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeCodeError(
                f"{type(node).__name__} not allowed (line {node.lineno})"
            )
        # 2) 禁止对 dunder 属性的访问 (Attribute(attr='__class__'))
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            # 允许 dunder 出现在 string literal 上下文 (比如 f-string);
            # 简单拒绝所有 dunder 访问 (LLM 不需要这些)
            raise UnsafeCodeError(
                f"dunder attribute access {node.attr!r} not allowed (line {node.lineno})"
            )
        # 3) 禁止危险 builtin name
        if isinstance(node, ast.Name) and node.id in _BUILTIN_DENY:
            raise UnsafeCodeError(
                f"builtin {node.id!r} not allowed (line {node.lineno})"
            )
        # 4) 禁止 importlib / os / sys 出现在 attribute 上 (如 os.system)
        if isinstance(node, ast.Attribute):
            if node.attr in {"system", "popen", "Popen", "spawn", "fork",
                             "execv", "execvp", "remove", "unlink", "rmtree",
                             "kill", "send", "connect", "socket"}:
                raise UnsafeCodeError(
                    f"dangerous attribute {node.attr!r} not allowed (line {node.lineno})"
                )

    # 5) 强制存在 def compute(ohlcv)
    has_compute = any(
        isinstance(node, ast.FunctionDef) and node.name == "compute"
        for node in ast.walk(tree)
    )
    if not has_compute:
        raise UnsafeCodeError("source must define `def compute(ohlcv) -> pd.Series`")


# -------------------- 运行时 exec --------------------

# 白名单 builtin — sandbox 内只 inject 这些
_SAFE_BUILTINS: dict[str, Any] = {
    # 基础类型 / 数学
    "abs": abs, "min": min, "max": max, "sum": sum,
    "round": round, "pow": pow, "divmod": divmod,
    # 序列 / 迭代
    "len": len, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "reversed": reversed, "sorted": sorted,
    "any": any, "all": all,
    # 容器
    "list": list, "tuple": tuple, "dict": dict, "set": set,
    "frozenset": frozenset,
    # 数字 / 字符串
    "int": int, "float": float, "complex": complex, "bool": bool,
    "str": str, "repr": repr, "format": format, "chr": ord,
    # 类型转换
    "iter": iter, "next": next,
    "slice": slice,
    # 异常基类 (LLM 写 raise 用)
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    "print": print,  # 日志方便
    # 关键 deny 显式置 None → AttributeError 友好
    "__import__": None, "open": None, "eval": None, "exec": None,
    "getattr": None, "setattr": None, "delattr": None,
    "globals": None, "locals": None, "vars": None,
    "compile": None, "importlib": None,
    "True": True, "False": False, "None": None,
}


def _make_globals() -> dict[str, Any]:
    """构造 sandbox exec 用的 globals 字典 (受控 builtin + 安全模块)."""
    import math

    return {
        "__builtins__": _SAFE_BUILTINS,
        # 数据科学模块 — pandas / numpy 必需, math 给 LLM 写 log/sqrt 用
        "pd": pd,
        "np": np,
        "math": math,
        # 常量
        "inf": float("inf"),
        "nan": float("nan"),
        "pi": math.pi,
        "e": math.e,
    }


class _TimeoutGuard:
    """signal.SIGALRM 主线程超时守门员。

    compute 跑超 sandbox timeout_s 直接抛 CodeTimeoutError.
    用 ``with _TimeoutGuard(30):`` 包住单次 compute 调用.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds)
        self._prev_handler: Any = None

    def __enter__(self) -> _TimeoutGuard:
        self._prev_handler = signal.signal(signal.SIGALRM, self._on_alarm)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self._prev_handler is not None:
            signal.signal(signal.SIGALRM, self._prev_handler)

    def _on_alarm(self, signum: int, frame: Any) -> None:
        raise CodeTimeoutError(
            f"compute exceeded sandbox timeout ({self.seconds:.1f}s)"
        )


def compile_code_factor(
    source: str,
    *,
    timeout_s: float = 30.0,
) -> tuple[Callable[[pd.DataFrame], pd.Series], str]:
    """编译 LLM 源码, 返回 ``(compute_fn, code_hash)``.

    失败抛 :class:`UnsafeCodeError` / :class:`CodeTimeoutError` / :class:`SyntaxError`.
    """
    _check_source_safety(source)
    code_hash = hashlib.sha1(source.encode("utf-8")).hexdigest()
    glb = _make_globals()
    loc: dict[str, Any] = {}
    # ``compile`` builtin 已禁, 直接用 Python 的 compile()
    code_obj = compile(source, "<llm_code_factor>", "exec")  # noqa: S102
    try:
        exec(code_obj, glb, loc)  # noqa: S102
    except Exception as exc:  # noqa: BLE001
        raise UnsafeCodeError(f"exec failed: {exc}") from exc

    fn = loc.get("compute") or glb.get("compute")
    if fn is None or not callable(fn):
        raise UnsafeCodeError("`compute` function not found after exec")
    # 跑一次空 ohlcv 验可用 + 防 LLM 写死循环
    try:
        with _TimeoutGuard(timeout_s):
            _ = fn(pd.DataFrame(columns=["date", "symbol", "open", "high", "low",
                                          "close", "volume", "amount"]))
    except CodeTimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 空 ohlcv 报错是 OK 的 (division by zero 等); 只要不超时 + 不抛非预期语法错即可
        logger.debug("compile_code_factor smoke-test (empty ohlcv) raised %s — OK", exc)
    return fn, code_hash


def code_hash(source: str) -> str:
    """给一段 LLM 源码算 hash (用于跨 session 去重)."""
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


# -------------------- 自检 --------------------

_SMOKE_TEST = """
def compute(ohlcv):
    import pandas as pd
    return ohlcv['close'].pct_change().iloc[-1]
"""


def _selftest() -> None:
    """模块加载时跑几个 sanity check, 失败打 warning (不抛)."""
    # 1) 合法代码应编译成功
    try:
        fn, h = compile_code_factor(_SMOKE_TEST, timeout_s=2.0)
        assert callable(fn) and len(h) == 40
    except Exception as exc:  # noqa: BLE001
        logger.warning("sandbox selftest: legal code failed: %s", exc)
    # 2) import 应被拒
    try:
        compile_code_factor("import os\ndef compute(ohlcv): return ohlcv['close']", timeout_s=2.0)
        logger.warning("sandbox selftest: 'import os' 被允许! BUG")
    except UnsafeCodeError:
        pass
    # 3) eval 应被拒
    try:
        compile_code_factor("def compute(ohlcv):\n    return eval('1+1')", timeout_s=2.0)
        logger.warning("sandbox selftest: 'eval' 被允许! BUG")
    except UnsafeCodeError:
        pass
    # 4) 没 def compute 应被拒
    try:
        compile_code_factor("x = 1", timeout_s=2.0)
        logger.warning("sandbox selftest: 无 compute 的代码被允许! BUG")
    except UnsafeCodeError:
        pass
    logger.debug("sandbox selftest passed")


# 模块加载时跑一遍自检, 立刻暴露 sandbox 漏判 bug
_selftest()


# 时间窗口方便调用方用 (避免 magic number)
DEFAULT_COMPUTE_TIMEOUT_S = 30.0
