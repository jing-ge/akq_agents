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
from akq_agents.web.api.stock import router as stock_router
from akq_agents.web.api.trading import router as trading_router
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
    app.include_router(trading_router, prefix="/api/trading", tags=["trading"])
    app.include_router(stock_router, prefix="/api/stock", tags=["stock"])

    # pages
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # 默认跳 research：用户每天最关心今日交易清单 + NAV，运维信息次要
        return RedirectResponse(url="/research")

    @app.get("/ops", response_class=HTMLResponse, include_in_schema=False)
    async def page_ops(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="ops.html.j2",
            context={"page": "ops", "ctx": _page_ctx()},
        )

    @app.get("/research", response_class=HTMLResponse, include_in_schema=False)
    async def page_research(request: Request) -> HTMLResponse:
        svc = get_services()
        factors = _collect_factors_for_dropdown(svc)
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
            # 过滤掉个股页专属 session（stock:XXXXXX:xxxxxxxx），它们只在
            # /stock/{symbol} 的 AI 抽屉里追问，不该混进主 chat 历史列表。
            sessions = [s for s in svc.llm_store.list_sessions(limit=50)
                        if not (s.get("session_id") or "").startswith("stock:")][:20]
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

    @app.get("/stock/{symbol}", response_class=HTMLResponse, include_in_schema=False)
    async def page_stock(request: Request, symbol: str) -> HTMLResponse:
        # 去掉可能的市场前缀，仅保留 6 位裸代码
        clean = str(symbol).strip().lower()
        for prefix in ("sh", "sz", "bj"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        if not clean.isdigit() or len(clean) != 6:
            # 无效 symbol 回研究页而不是抛 404（UX 引导 + 避免 crawler 触发大量 404 log）
            return RedirectResponse(url="/research", status_code=307)  # type: ignore[return-value]
        return templates.TemplateResponse(
            request=request, name="stock.html.j2",
            context={"page": "stock", "ctx": _page_ctx(), "symbol": clean},
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
        "boot_ts": _BOOT_TS,
    }


_BUILTIN_PREFIXES = ("momentum_", "reversal_", "volatility_", "amount_", "log_amount_")


def _classify_factor_status(name: str, registry_names: set, p_status: str | None) -> str:
    """因子状态: builtin / accepted / shadow / rejected / demoted."""
    if any(name.startswith(p) for p in _BUILTIN_PREFIXES):
        return "builtin"
    if p_status is None:
        return "accepted" if name in registry_names else "unknown"
    return p_status


def _collect_factors_for_dropdown(svc) -> list[dict]:
    """M19: 因子表现下拉框 union 全集 (builtin + accepted + shadow + 历史 rejected/demoted),
    跳过 rejected.compute_error (recipe 跑不出值的死因子)。

    跟 /api/research/factors 同一份数据源, 但这里只取展示需要的字段 (name + factor_version +
    direction + status), 给模板按 status 分组渲染 optgroup 用。
    """
    if svc.factor_registry is None:
        return []
    registry = {f.name: f for f in svc.factor_registry.list_all()}
    registry_names = set(registry.keys())

    # 拉 proposal_store 里的所有 status (含 shadow / 历史 rejected / demoted)
    proposal_rows: dict[str, tuple[str, str | None, str | None, int | None]] = {}
    if svc.proposal_store is not None and svc.repo is not None:
        try:
            from akq_agents.services.data.repository import open_meta_db
            db_path = svc.repo._base_dir / "meta.db"
            with open_meta_db(db_path) as conn:
                for name, status, direction, reason, ver in conn.execute(
                    """
                    SELECT factor_name, status, direction, reason,
                           1 AS factor_version
                    FROM factor_proposals
                    WHERE status IN ('accepted', 'shadow', 'rejected', 'demoted')
                      AND evicted_at IS NULL
                    """
                ).fetchall():
                    # 跳过 compute_error 类
                    if status == "rejected" and reason and reason.startswith("compute_error"):
                        continue
                    proposal_rows[name] = (status, direction, reason, ver)
        except Exception:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger(__name__).warning("_collect_factors_for_dropdown: read proposals failed",
                                            exc_info=True)

    rows: list[dict] = []
    seen: set = set()
    # 1) 先 registry 里的 (有完整 Factor 实例)
    for name, f in registry.items():
        seen.add(name)
        p_status = proposal_rows.get(name, (None,))[0]
        rows.append({
            "name": name,
            "factor_version": f.factor_version,
            "direction": f.direction,
            "status": _classify_factor_status(name, registry_names, p_status),
        })
    # 2) proposal_store 里 registry 没有的 (shadow / 历史 rejected / demoted)
    for name, (status, direction, _reason, ver) in proposal_rows.items():
        if name in seen:
            continue
        rows.append({
            "name": name,
            "factor_version": ver or 1,
            "direction": direction or "",
            "status": _classify_factor_status(name, registry_names, status),
        })

    # 排序: builtin → accepted → shadow → rejected → demoted; 同组内按名字
    status_order = {"builtin": 0, "accepted": 1, "shadow": 2, "rejected": 3, "demoted": 4, "unknown": 9}
    rows.sort(key=lambda r: (status_order.get(r["status"], 99), r["name"]))
    return rows


# 进程级 boot 时间戳，用于 /static/*.css?v=... 强制刷新缓存
_BOOT_TS = str(int(__import__("time").time()))


# uvicorn 直接调用的入口
app = create_app()
