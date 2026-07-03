"""Stock 个股详情 endpoints。

- ``GET /api/stock/overview/{symbol}`` — hero 卡数据
- ``GET /api/stock/kline/{symbol}?period=D|W|M|Y|1m..120m&limit=250`` — K 线
- ``GET /api/stock/intraday/{symbol}?days=1|5`` — 分时 / 五日分时
- ``POST /api/stock/analyze/{symbol}`` — 一键 AI 分析（复用 LLMOrchestrator.run_analyst）
- ``GET /api/stock/search?q=xxx&limit=8`` — 顶部搜索框 suggest

追问框直接复用现有 ``POST /api/chat/sessions/{session_id}/messages``，本 module 不重复实现。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from akq_agents.web.deps import ServiceContainer, get_services

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程级 StockDetailService 单例，跨请求共享内存缓存
_service_singleton = None


def _get_service():
    global _service_singleton
    if _service_singleton is not None:
        return _service_singleton
    svc: ServiceContainer = get_services()
    if svc.repo is None:
        raise HTTPException(503, detail="data repository not ready")
    # 尝试从 repo 拿 StockNameStore；没有就 fall back None（name 字段会是 None）
    name_store = None
    try:
        from akq_agents.services.data.stock_names import StockNameStore
        db_path = svc.repo._base_dir / "meta.db"
        name_store = StockNameStore(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("StockNameStore init failed: %s", exc)

    # 本地 industry 兜底 map (SQLite meta.db.industry_map). em 挂时 hero 卡
    # 的 industry 字段依赖它填. 加载失败也不 raise, 只是 industry 会 None.
    industry_map: dict[str, str] = {}
    try:
        from akq_agents.services.portfolio.industry_map import IndustryMapStore
        db_path = svc.repo._base_dir / "meta.db"
        industry_map = IndustryMapStore(db_path).load_names()
        logger.info("IndustryMapStore loaded %d entries for stock_detail", len(industry_map))
    except Exception as exc:  # noqa: BLE001
        logger.warning("IndustryMapStore load failed: %s", exc)

    from akq_agents.services.stock_detail import StockDetailService
    _service_singleton = StockDetailService(
        repo=svc.repo, name_store=name_store, industry_map=industry_map,
    )
    return _service_singleton


def _reset_service_for_tests() -> None:
    """测试钩子：清空单例让 fixture 每次重建。"""
    global _service_singleton
    _service_singleton = None


# ============================================================================


@router.get("/overview/{symbol}")
async def get_overview(
    symbol: str,
    fast: bool = Query(default=False, description="只走本地数据源, 跳过所有 akshare 网络调用. 首屏推荐."),
) -> dict[str, Any]:
    symbol = _clean_symbol(symbol)
    service = _get_service()
    if fast:
        overview = service.fetch_overview_quick(symbol)
    else:
        overview = service.fetch_overview(symbol)
    return overview.to_dict()


@router.get("/kline/{symbol}")
async def get_kline(
    symbol: str,
    period: str = Query(default="D", pattern=r"^(D|W|M|Y|1m|5m|15m|30m|60m|120m)$"),
    limit: int = Query(default=250, ge=1, le=1000),
) -> dict[str, Any]:
    symbol = _clean_symbol(symbol)
    service = _get_service()
    try:
        return service.fetch_kline(symbol, period, limit)
    except RuntimeError as exc:
        # akshare 分钟接口失败 → 502
        raise HTTPException(502, detail=f"upstream failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/intraday/{symbol}")
async def get_intraday(
    symbol: str,
    days: int = Query(default=1, ge=1, le=5),
) -> dict[str, Any]:
    """分时 / 五日分时.

    akshare 分钟接口本地网络稳定性差, 服务层已内部重试 3 次. 仍失败时
    返回 200 + points=[] + error 字段, 让前端以'暂不可用'替代整页崩溃.
    """
    symbol = _clean_symbol(symbol)
    service = _get_service()
    try:
        return service.fetch_intraday(symbol, days)
    except RuntimeError as exc:
        # 上游降级: 返回 200 + 空点 + error, 前端友好展示
        return {
            "symbol": symbol,
            "days": days,
            "points": [],
            "error": "upstream_unavailable",
            "detail": str(exc),
        }


class AnalyzeRequest(BaseModel):
    period_context: str = "D"
    model: str | None = None


@router.post("/analyze/{symbol}")
async def post_analyze(symbol: str, body: AnalyzeRequest | None = None) -> dict[str, Any]:
    symbol = _clean_symbol(symbol)
    svc: ServiceContainer = get_services()
    if svc.llm_orchestrator is None or svc.llm_config is None or svc.llm_store is None:
        raise HTTPException(503, detail="llm not configured")

    body = body or AnalyzeRequest()
    service = _get_service()

    from akq_agents.agents.stock_analyst_agent import analyze
    from akq_agents.services.llm.client import LLMGatewayError

    try:
        result = analyze(
            symbol=symbol,
            detail_service=service,
            llm_orchestrator=svc.llm_orchestrator,
            llm_config=svc.llm_config,
            llm_store=svc.llm_store,
            period_context=body.period_context,
            disclaimer_header=getattr(svc.llm_config.safety, "disclaimer_header", None) if svc.llm_config else None,
            model_override=body.model,
        )
    except LLMGatewayError as exc:
        raise HTTPException(
            502, detail={"reason_code": exc.reason_code, "message": str(exc)[:300]}
        ) from exc
    return result


@router.get("/search")
async def get_search(
    q: str = Query(default="", max_length=32),
    limit: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    service = _get_service()
    matches = service.search(q, limit=limit)
    return {"matches": matches, "n": len(matches), "query": q}


# ============================================================================


def _clean_symbol(symbol: str) -> str:
    """去掉 sh/sz/bj 前缀，只留 6 位数字代码。"""
    s = str(symbol).strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if not s.isdigit() or len(s) != 6:
        raise HTTPException(400, detail=f"invalid symbol: {symbol!r}")
    return s
