"""
模块: api.routes.news
职责: 内容接口——资讯库、报告、推送记录查询
依赖: api.deps, api.serializers, core.models
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select

from newsbot.api.deps import Authed, Services, get_services
from newsbot.api.serializers import delivery_dict, news_dict, report_dict
from newsbot.core.models import Delivery, NewsItem, Report

router = APIRouter(tags=["content"], dependencies=[Authed])

MAX_LIMIT = 200


@router.get("/news")
async def list_news(
    services: Services = Depends(get_services),
    keyword: str | None = None,
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """资讯库：按抓取时间倒序，可按标题或网址关键词过滤。"""
    since = datetime.now(UTC) - timedelta(days=days)
    conditions: list[Any] = [NewsItem.fetched_at >= since]
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(or_(NewsItem.title.like(pattern), NewsItem.url.like(pattern)))
    async with services.db.session() as session:
        total = (await session.execute(select(func.count(NewsItem.id)).where(*conditions))).scalar_one()
        rows = (
            (
                await session.execute(
                    select(NewsItem).where(*conditions).order_by(NewsItem.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
    return {"total": total, "items": [news_dict(row) for row in rows]}


@router.get("/reports")
async def list_reports(
    services: Services = Depends(get_services),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """生成的日报与答复，最新在前。"""
    async with services.db.session() as session:
        total = (await session.execute(select(func.count(Report.id)))).scalar_one()
        rows = (
            (await session.execute(select(Report).order_by(Report.id.desc()).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
    return {"total": total, "items": [report_dict(row) for row in rows]}


@router.get("/deliveries")
async def list_deliveries(
    services: Services = Depends(get_services),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """推送记录，含 waiting_user（等用户下次发消息时补投）。"""
    conditions = [Delivery.status == status_filter] if status_filter else []
    async with services.db.session() as session:
        total = (
            await session.execute(
                select(func.count(Delivery.id)).where(*conditions) if conditions else select(func.count(Delivery.id))
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Delivery).where(*conditions).order_by(Delivery.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
    return {"total": total, "items": [delivery_dict(row) for row in rows]}
