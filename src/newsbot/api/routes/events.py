"""
模块: api.routes.events
职责: 事件层接口——事件列表、事件详情、热度榜、日报编排
依赖: api.deps, api.serializers, core.models, result.digest, result.hot
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from newsbot.api.deps import Authed, Services, get_services
from newsbot.api.serializers import report_item_dict, story_dict
from newsbot.core.models import NewsItem, ReportItem, Story, StoryScore
from newsbot.result import digest as digest_mod
from newsbot.result import hot

router = APIRouter(tags=["events"], dependencies=[Authed])

MAX_LIMIT = 200
# 取热度榜时放宽条数：事件列表里每个事件都要有热度，只取前 20 会让后面的事件显示空
HEAT_LOOKUP = 500


@router.get("/events")
async def list_events(
    services: Services = Depends(get_services),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(30, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """事件列表：一段时间内有新报道的事件，按最近有新报道的时间倒序。

    排序用"最近有新报道"而不是首次出现时间：一个上周开始、今天有进展的事件应该排在
    今天没有进展的旧事件前面。
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with services.db.session() as session:
        rows = (
            await session.scalars(
                select(Story).where(Story.last_seen_at >= since).order_by(Story.last_seen_at.desc()).limit(limit)
            )
        ).all()
        ids = [row.id for row in rows]
        counts = await _participants(session, ids)
    ranking = {row.story_id: row for row in await hot.ranking(services.db, limit=HEAT_LOOKUP)}
    items = []
    for row in rows:
        heat_row = ranking.get(row.id)
        items.append(
            story_dict(
                row,
                heat=heat_row.heat if heat_row else None,
                participants=counts.get(row.id, heat_row.participants if heat_row else 0),
            )
        )
    return {"total": len(items), "items": items}


@router.get("/events/{story_id}")
async def get_event(services: Services = Depends(get_services), story_id: int = 0) -> dict[str, Any]:
    """事件详情：这件事的全部报道，含角色（一手 / 后续 / 重复）与精选分。

    角色直接给前端：读者要能一眼看出这件事现在到哪一步了。
    """
    async with services.db.session() as session:
        story = await session.get(Story, story_id)
        if story is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        rows = (
            await session.execute(
                select(ReportItem, NewsItem, StoryScore)
                .join(NewsItem, NewsItem.id == ReportItem.news_item_id)
                .outerjoin(StoryScore, StoryScore.news_item_id == ReportItem.news_item_id)
                .where(ReportItem.story_id == story_id)
                .order_by(ReportItem.observed_at)
            )
        ).all()
        counts = await _participants(session, [story_id])
    ranking = {row.story_id: row for row in await hot.ranking(services.db, limit=HEAT_LOOKUP)}
    heat_row = ranking.get(story_id)
    return {
        "story": story_dict(
            story,
            heat=heat_row.heat if heat_row else None,
            participants=counts.get(story_id, heat_row.participants if heat_row else 0),
        ),
        "reports": [report_item_dict(report, item, score) for report, item, score in rows],
    }


@router.get("/hot")
async def hot_ranking(
    services: Services = Depends(get_services),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """热度榜：48 小时窗口、24 小时半衰期、按独立参与者计数。"""
    moment = datetime.now(UTC)
    rows = await hot.ranking(services.db, moment, limit=limit)
    return {
        "updated_at": moment.isoformat(),
        "window_hours": hot.WINDOW_HOURS,
        "half_life_hours": hot.HALF_LIFE_HOURS,
        "rule": hot.RULE_VERSION,
        "items": [
            {
                "story_id": row.story_id,
                "title": row.title,
                "heat": row.heat,
                "index": row.index,
                "participants": row.participants,
                "sources": row.sources,
                "trend": row.trend,
                "trend_pct": row.trend_pct,
                "badges": list(row.badges),
                "latest_at": row.latest_at.isoformat() if row.latest_at else None,
                "first_at": row.first_at.isoformat() if row.first_at else None,
            }
            for row in rows
        ],
    }


@router.get("/digest")
async def get_digest(
    services: Services = Depends(get_services),
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    limit: int = Query(digest_mod.DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    """日报/周报/月报。编排是纯规则，所以这个接口可以随便刷新、结果稳定。"""
    built = await digest_mod.assemble(services.db, period=period, limit=limit, timezone=services.config.app.timezone)
    return {
        "period": built.period,
        "title": built.title,
        "since": built.since.isoformat(),
        "until": built.until.isoformat(),
        "skipped": built.skipped,
        "items": [
            {
                "story_id": entry.story_id,
                "title": entry.title,
                "digest": entry.digest,
                "url": entry.url,
                "score": entry.score,
                "participants": entry.participants,
                "official": entry.official,
                "follow_up_only": entry.follow_up_only,
                "heat": entry.heat,
                "importance": entry.importance,
                "latest_at": entry.latest_at.isoformat() if entry.latest_at else None,
                "also_reported": entry.titles[1:4],
            }
            for entry in built.entries
        ],
    }


async def _participants(session: Any, story_ids: list[int]) -> dict[int, int]:
    """每个事件的**独立参与者**数量（去重后的 participant）。

    注意不是"报道条数"：一家媒体发十篇只算一个参与者——展示条数会让榜单被单个媒体的
    刷屏占满，这正是热度算法要避免的事情，页面口径必须与算法一致。
    """
    if not story_ids:
        return {}
    rows = (
        await session.execute(
            select(ReportItem.story_id, ReportItem.participant).where(ReportItem.story_id.in_(story_ids))
        )
    ).all()
    grouped: dict[int, set[str]] = {}
    for story_id, participant in rows:
        name = str(participant or "").strip()
        if name:
            grouped.setdefault(story_id, set()).add(name)
    return {story_id: len(names) for story_id, names in grouped.items()}
