"""测试 services/factors/sandbox.py — LLM 因子代码沙箱.

覆盖:
- 合法代码应编译成功, compute 返回 pd.Series
- import / eval / open / __import__ / 危险属性 / dunder 全部被静态拒绝
- 无 def compute 的代码被拒
- compute 跑超时应抛 CodeTimeoutError
- compute 跑空 ohlcv 不应爆 (允许 NaN/异常, 不允许 timeout)
- 跨 session 相同 source_code 算同 code_hash
- CodeFactor.compute 调用 sandbox fn 失败时返回全 NaN 而不污染上层
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from akq_agents.services.factors.base import CodeFactor
from akq_agents.services.factors.sandbox import (
    CodeTimeoutError,
    UnsafeCodeError,
    code_hash,
    compile_code_factor,
)


# ---------------- 合法代码 ----------------


def test_compile_legal_simple():
    source = """
def compute(ohlcv):
    wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()
    return wide.pct_change(20).iloc[-1]
"""
    fn, h = compile_code_factor(source, timeout_s=5.0)
    assert callable(fn)
    assert len(h) == 40
    assert isinstance(h, str)


def test_compile_legal_uses_np_and_math():
    source = """
def compute(ohlcv):
    wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()
    log_ret = np.log(wide / wide.shift(1))
    return log_ret.std() * math.sqrt(252)
"""
    fn, h = compile_code_factor(source, timeout_s=5.0)
    assert callable(fn)


# ---------------- 危险代码拒绝 ----------------


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef compute(ohlcv): return ohlcv['close']",
        "from os import system\ndef compute(ohlcv): return ohlcv['close']",
        "def compute(ohlcv):\n    return eval('1+1')",
        "def compute(ohlcv):\n    return exec('1+1')",
        "def compute(ohlcv):\n    return open('/etc/passwd')",
        "def compute(ohlcv):\n    return __import__('os').system('echo hi')",
        "def compute(ohlcv):\n    return getattr(ohlcv, '__class__')",
        "def compute(ohlcv):\n    return globals()",
        "def compute(ohlcv):\n    ohlcv.__class__",
        "import subprocess\ndef compute(ohlcv): return ohlcv['close']",
        "x = 1",  # 没 def compute
    ],
)
def test_unsafe_code_rejected(source):
    with pytest.raises(UnsafeCodeError):
        compile_code_factor(source, timeout_s=2.0)


def test_dangerous_attribute_rejected():
    # os.system (os 本身 import 已拦, 但 attribute 二次防御)
    source = "def compute(ohlcv):\n    x = 'os'\n    return x.system"
    # 上面的 'os' 是字符串, attribute .system 在 x 上 — 也被拒
    with pytest.raises(UnsafeCodeError):
        compile_code_factor(source, timeout_s=2.0)


# ---------------- 超时 ----------------


def test_compute_smoke_test_timeout():
    # compile 阶段跑空 ohlcv 时死循环 → 应抛 CodeTimeoutError
    source = """
def compute(ohlcv):
    while True:
        pass
"""
    with pytest.raises(CodeTimeoutError):
        compile_code_factor(source, timeout_s=1.0)


# ---------------- CodeFactor.compute 容错 ----------------


def test_code_factor_compute_returns_series_on_empty_ohlcv():
    # 给个 compute 跑空 ohlcv 必报错的实现, CodeFactor 兜底返全 NaN
    source = """
def compute(ohlcv):
    return 1 / len(ohlcv)
"""
    fn, h = compile_code_factor(source, timeout_s=2.0)
    factor = CodeFactor(
        name="test_zero_div", source_code=source, fn=fn,
        direction="long", code_hash=h,
    )
    # 空 ohlcv — CodeFactor.compute 兜底返全 NaN
    empty = pd.DataFrame(columns=["date", "symbol", "close"])
    out = factor.compute(empty)
    assert isinstance(out, pd.Series)
    assert out.name == "test_zero_div"
    # 全 NaN / 空
    assert len(out) == 0 or out.isna().all()


def test_code_factor_compute_works_on_real_ohlcv():
    source = """
def compute(ohlcv):
    wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()
    return wide.pct_change(5).iloc[-1]
"""
    fn, h = compile_code_factor(source, timeout_s=2.0)
    factor = CodeFactor(
        name="code_pct5", source_code=source, fn=fn,
        direction="long", code_hash=h,
    )
    dates = pd.date_range("2026-06-01", periods=20, freq="D")
    rows = []
    for d in dates:
        for sym in ["000001.SZ", "000002.SZ", "600000.SH"]:
            rows.append({
                "date": d, "symbol": sym,
                "open": 10.0, "high": 11.0, "low": 9.0,
                "close": 10.0 + np.random.RandomState(0).rand(),
                "volume": 1_000_000, "amount": 10_000_000.0,
            })
    ohlcv = pd.DataFrame(rows)
    out = factor.compute(ohlcv)
    assert isinstance(out, pd.Series)
    assert out.name == "code_pct5"
    assert set(out.index) == {"000001.SZ", "000002.SZ", "600000.SH"}


def test_code_factor_compute_runtime_exception_returns_nan():
    # fn 跑起来抛异常的极端情况 (sandbox 编译过了, 真实数据爆) → CodeFactor 兜底
    source = """
def compute(ohlcv):
    if ohlcv.empty:
        raise RuntimeError("simulated runtime failure")
    return ohlcv['close']
"""
    fn, h = compile_code_factor(source, timeout_s=2.0)
    # 上面 compile 时已经过 smoke test (空 ohlcv) → 已抛 RuntimeError 被吞掉
    factor = CodeFactor(
        name="rt_fail", source_code=source, fn=fn,
        direction="long", code_hash=h,
    )
    # 实际 compute 一个空 ohlcv → fn 抛异常 → CodeFactor.compute 兜底返全 NaN
    out = factor.compute(pd.DataFrame(columns=["date", "symbol", "close"]))
    assert isinstance(out, pd.Series)
    # 没 symbol 时 Series 是空
    assert len(out) == 0


# ---------------- code_hash 跨 session 稳定 ----------------


def test_code_hash_stable():
    src = "def compute(ohlcv):\n    return ohlcv['close']"
    h1 = code_hash(src)
    h2 = code_hash(src)
    assert h1 == h2
    assert len(h1) == 40  # sha1
    assert h1 != code_hash(src + " ")  # 加空格 hash 必变


# ---------------- performance sanity (不需要测) ----------------


def test_compile_speed_is_fast():
    """合法的普通 compute 编译应 < 0.5s."""
    source = """
def compute(ohlcv):
    wide = ohlcv.pivot_table(index='date', columns='symbol', values='close').sort_index()
    return wide.pct_change(20).iloc[-1]
"""
    t0 = time.time()
    compile_code_factor(source, timeout_s=2.0)
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"compile 耗时 {elapsed:.3f}s, 应 < 0.5s"