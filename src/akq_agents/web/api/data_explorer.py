"""Data Explorer endpoints：/api/data/catalog + /api/data/fetch。

提供 akshare 数据可视化板块所需的两个 endpoint。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

# 进程级 service 单例（带 5min TTL 缓存）
_service = None


def _get_service():
    global _service
    if _service is None:
        from akq_agents.services.data_explorer import DataExplorerService
        _service = DataExplorerService()
    return _service


@router.get("/catalog")
async def get_catalog() -> dict[str, Any]:
    """返回 akshare 接口白名单 + 按 category 分组。"""
    svc = _get_service()
    items = svc.list_catalog()
    # 按 category 分组方便前端渲染
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    return {"groups": groups, "total": len(items)}


@router.post("/fetch")
async def fetch_data(payload: dict[str, Any]) -> dict[str, Any]:
    """拉取某个接口的数据。

    payload 形如 ``{"api": "stock_zh_a_daily", "args": {"symbol": "sh600519", "adjust": "qfq"}}``
    """
    api = payload.get("api")
    args = payload.get("args") or {}
    if not api:
        raise HTTPException(400, "缺少 api 字段")
    svc = _get_service()
    result = svc.fetch(api, args)
    if "error" in result:
        # 包成 400 让前端能感知
        raise HTTPException(400, f"{result.get('error')}: {result.get('detail', '')}")
    return result
