"""FastAPI application factory（``akq_agents.web.app``）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from akq_agents.web.api.chat import router as chat_router
from akq_agents.web.api.control import router as control_router
from akq_agents.web.api.data_explorer import router as data_router
from akq_agents.web.api.discovery import router as discovery_router
from akq_agents.web.api.ops import router as ops_router
from akq_agents.web.api.research import router as research_router
from akq_agents.web.deps import get_services
from akq_agents.web.guard import LocalhostOnlyMiddleware

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def create_app() -> FastAPI:
    """构造 FastAPI app；所有路由 + middleware 在此注册。"""
    app = FastAPI(title="AKQ Agents Console", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalhostOnlyMiddleware)

    # static
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # api routers
    app.include_router(ops_router, prefix="/api/ops", tags=["ops"])
    app.include_router(research_router, prefix="/api/research", tags=["research"])
    app.include_router(discovery_router, prefix="/api/research", tags=["discovery"])
    app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
    app.include_router(control_router, prefix="/api/control", tags=["control"])
    app.include_router(data_router, prefix="/api/data", tags=["data"])

    # pages
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ops")

    @app.get("/ops", response_class=HTMLResponse, include_in_schema=False)
    async def page_ops(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="ops.html.j2",
            context={"page": "ops", "ctx": _page_ctx()},
        )

    @app.get("/research", response_class=HTMLResponse, include_in_schema=False)
    async def page_research(request: Request) -> HTMLResponse:
        svc = get_services()
        factors = []
        if svc.factor_registry is not None:
            factors = [
                {
                    "name": f.name,
                    "factor_version": f.factor_version,
                    "direction": f.direction,
                }
                for f in svc.factor_registry.list_all()
            ]
        snapshot_dates: list[str] = []
        if svc.portfolio_store is not None:
            snapshot_dates = svc.portfolio_store.list_dates(limit=30)
        return templates.TemplateResponse(
            request=request, name="research.html.j2",
            context={
                "page": "research",
                "ctx": _page_ctx(),
                "factors": factors,
                "snapshot_dates": snapshot_dates,
            },
        )

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    async def page_chat(request: Request) -> HTMLResponse:
        svc = get_services()
        sessions = []
        if svc.llm_store is not None:
            sessions = svc.llm_store.list_sessions(limit=20)
        return templates.TemplateResponse(
            request=request, name="chat.html.j2",
            context={"page": "chat", "ctx": _page_ctx(), "sessions": sessions},
        )

    @app.get("/data", response_class=HTMLResponse, include_in_schema=False)
    async def page_data(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="data.html.j2",
            context={"page": "data", "ctx": _page_ctx()},
        )

    @app.get("/logs", response_class=HTMLResponse, include_in_schema=False)
    async def page_logs(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="logs.html.j2",
            context={"page": "logs", "ctx": _page_ctx()},
        )

    return app


def _page_ctx() -> dict:
    svc = get_services()
    web_cfg = svc.web_config
    return {
        "title": (web_cfg.ui.title if web_cfg else "AKQ Agents Console"),
        "timezone": (web_cfg.ui.timezone if web_cfg else "Asia/Shanghai"),
        "echarts_cdn_url": (web_cfg.echarts.cdn_url if web_cfg else "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"),
        "echarts_use_cdn": (web_cfg.echarts.use_cdn if web_cfg else True),
        "poll_intervals_ms": (web_cfg.poll_intervals_ms.model_dump() if web_cfg else {"ops_health": 5000, "ops_jobs": 5000, "ops_events": 3000}),
    }


# uvicorn 直接调用的入口
app = create_app()
