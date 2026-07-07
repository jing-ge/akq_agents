"""测试 web/guard.py 的 CSRFOriginMiddleware — 写操作 Origin/Referer 校验。

覆盖:
- 同源写 (Origin=loopback) 放行
- 跨源写 (Origin=外部域) 403
- 无 Origin/Referer 头 (CLI/curl) 放行 — CSRF 不适用于非浏览器客户端
- 安全方法 GET 不受校验 (即使带外部 Origin)
- Referer 也参与校验
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from akq_agents.web.guard import CSRFOriginMiddleware


def _make_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CSRFOriginMiddleware)

    @app.post("/write")
    async def write() -> dict:
        return {"ok": True}

    @app.get("/read")
    async def read() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_same_origin_write_allowed():
    c = _make_client()
    r = c.post("/write", headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_cross_origin_write_rejected():
    c = _make_client()
    r = c.post("/write", headers={"origin": "http://evil.com"})
    assert r.status_code == 403
    assert "cross-origin" in r.json()["error"]


def test_no_origin_header_write_allowed():
    """非浏览器客户端 (CLI/curl/内部脚本) 不带 Origin, 应放行。"""
    c = _make_client()
    r = c.post("/write")
    assert r.status_code == 200


def test_safe_method_not_checked():
    """GET 是安全方法, 即使带外部 Origin 也放行。"""
    c = _make_client()
    r = c.get("/read", headers={"origin": "http://evil.com"})
    assert r.status_code == 200


def test_localhost_referer_allowed():
    c = _make_client()
    r = c.post("/write", headers={"referer": "http://localhost:8765/research"})
    assert r.status_code == 200


def test_cross_origin_referer_rejected():
    c = _make_client()
    r = c.post("/write", headers={"referer": "http://attacker.example/page"})
    assert r.status_code == 403
