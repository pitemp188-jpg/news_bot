"""
模块: api.routes.schedules
职责: 定时任务接口——列表、新建、改时间与主题、启停
依赖: api.deps, api.serializers, core.models, dispatcher.scheduler
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from newsbot.api.deps import Authed, Services, get_services
from newsbot.api.serializers import schedule_dict
from newsbot.core.models import Schedule
from newsbot.dispatcher.scheduler import cron_from_time

router = APIRouter(prefix="/schedules", tags=["schedules"], dependencies=[Authed])

_CRON_RE = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")
MAX_TOPICS = 5


class ScheduleCreate(BaseModel):
    """新建定时推送：时间可用 HH:MM 或五段 cron。"""

    topics: list[str] = Field(min_length=1, max_length=MAX_TOPICS)
    time: str | None = None
    cron: str | None = None
    platform: str
    chat_id: str

    @field_validator("topics")
    @classmethod
    def _clean_topics(cls, value: list[str]) -> list[str]:
        cleaned = [topic.strip() for topic in value if topic.strip()]
        if not cleaned:
            raise ValueError("主题不能为空")
        return cleaned


class ScheduleUpdate(BaseModel):
    """增量修改：只改传进来的字段。"""

    topics: list[str] | None = Field(None, min_length=1, max_length=MAX_TOPICS)
    time: str | None = None
    cron: str | None = None
    enabled: bool | None = None


def _resolve_cron(time_text: str | None, cron: str | None) -> str:
    if cron:
        if not _CRON_RE.match(cron.strip()):
            raise HTTPException(422, "cron 需要五段格式，例如 0 21 * * *")
        return cron.strip()
    try:
        return cron_from_time(time_text or "")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _get_schedule(services: Services, schedule_id: int) -> Schedule:
    async with services.db.session() as session:
        row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"订阅 #{schedule_id} 不存在")
    return row


async def _sync(services: Services) -> None:
    """改动后立刻同步调度器，避免要等重启才生效。"""
    if services.app.scheduler is not None:
        await services.app.scheduler.sync()


@router.get("")
async def list_schedules(services: Services = Depends(get_services)) -> dict[str, Any]:
    async with services.db.session() as session:
        rows = (await session.execute(select(Schedule).order_by(Schedule.id))).scalars().all()
    return {"items": [schedule_dict(row) for row in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(payload: ScheduleCreate, services: Services = Depends(get_services)) -> dict[str, Any]:
    cron = _resolve_cron(payload.time, payload.cron)
    async with services.db.session() as session:
        row = Schedule(
            cron=cron, topics=payload.topics, platform=payload.platform, chat_id=payload.chat_id, enabled=True
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    await _sync(services)
    return schedule_dict(row)


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: int, payload: ScheduleUpdate, services: Services = Depends(get_services)
) -> dict[str, Any]:
    """改主题、改时间或启停；时间与 cron 同时传时以 cron 为准。"""
    await _get_schedule(services, schedule_id)
    cron = None
    if payload.cron or payload.time:
        cron = _resolve_cron(payload.time, payload.cron)
    async with services.db.session() as session:
        row = await session.get(Schedule, schedule_id)
        assert row is not None
        if payload.topics is not None:
            row.topics = [topic.strip() for topic in payload.topics if topic.strip()]
        if cron is not None:
            row.cron = cron
        if payload.enabled is not None:
            row.enabled = payload.enabled
        await session.commit()
        await session.refresh(row)
    await _sync(services)
    return schedule_dict(row)


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: int, purge: bool = False, services: Services = Depends(get_services)
) -> dict[str, Any]:
    """默认停用（保留历史，和 /退订 一致）；purge=true 才真正删除。"""
    await _get_schedule(services, schedule_id)
    async with services.db.session() as session:
        row = await session.get(Schedule, schedule_id)
        assert row is not None
        if purge:
            await session.delete(row)
            await session.commit()
            await _sync(services)
            return {"deleted": schedule_id}
        row.enabled = False
        await session.commit()
        await session.refresh(row)
    await _sync(services)
    return schedule_dict(row)
