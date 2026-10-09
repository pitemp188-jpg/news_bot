"""
模块: api.routes.tasks
职责: 任务接口——列表、详情、手动提交、取消、重跑
依赖: api.deps, api.serializers, core.db, core.models, dispatcher.queue
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from newsbot.api.deps import Authed, Services, get_services
from newsbot.api.serializers import delivery_dict, report_dict, task_dict
from newsbot.core.models import Delivery, Report, Task

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Authed])

MAX_LIMIT = 200
TERMINAL = ("succeeded", "failed", "timeout", "cancelled")


class SubmitTask(BaseModel):
    """手动提交一个查询任务。"""

    query: str = Field(min_length=1, max_length=2000)
    platform: str | None = None
    chat_id: str | None = None


async def _get_task(services: Services, task_id: int) -> Task:
    async with services.db.session() as session:
        row = await session.get(Task, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"任务 #{task_id} 不存在")
    return row


@router.get("")
async def list_tasks(
    services: Services = Depends(get_services),
    status_filter: str | None = Query(None, alias="status"),
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """按状态与类型过滤任务，最新的排在前面。"""
    conditions = []
    if status_filter:
        conditions.append(Task.status == status_filter)
    if kind:
        conditions.append(Task.kind == kind)
    async with services.db.session() as session:
        total = (
            await session.execute(
                select(func.count(Task.id)).where(*conditions) if conditions else select(func.count(Task.id))
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Task).where(*conditions).order_by(Task.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
    return {"total": total, "items": [task_dict(row) for row in rows]}


@router.get("/{task_id}")
async def task_detail(task_id: int, services: Services = Depends(get_services)) -> dict[str, Any]:
    """任务详情：附带生成的报告与投递记录。"""
    row = await _get_task(services, task_id)
    async with services.db.session() as session:
        reports = (
            (await session.execute(select(Report).where(Report.task_id == task_id).order_by(Report.id))).scalars().all()
        )
        report_ids = [report.id for report in reports]
        deliveries = (
            (await session.execute(select(Delivery).where(Delivery.report_id.in_(report_ids)).order_by(Delivery.id)))
            .scalars()
            .all()
            if report_ids
            else []
        )
    return {
        "task": task_dict(row),
        "reports": [report_dict(item) for item in reports],
        "deliveries": [delivery_dict(item) for item in deliveries],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_task(payload: SubmitTask, services: Services = Depends(get_services)) -> dict[str, Any]:
    """手动跑一次查询；不带目标时只生成报告不投递。"""
    queue = services.app.queue
    if queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "任务队列未就绪")
    task = await queue.submit(
        kind="manual", query=payload.query.strip(), platform=payload.platform, chat_id=payload.chat_id
    )
    return task_dict(task)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, services: Services = Depends(get_services)) -> dict[str, Any]:
    """取消等待中或执行中的任务。"""
    queue = services.app.queue
    if queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "任务队列未就绪")
    await _get_task(services, task_id)
    cancelled = await queue.cancel(task_id)
    if not cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"任务 #{task_id} 已结束，无法取消")
    return task_dict(await _get_task(services, task_id))


@router.post("/{task_id}/retry")
async def retry_task(task_id: int, services: Services = Depends(get_services)) -> dict[str, Any]:
    """重跑一个已结束的任务。"""
    queue = services.app.queue
    if queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "任务队列未就绪")
    row = await _get_task(services, task_id)
    if row.status not in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"任务 #{task_id} 仍在执行，无需重跑")
    if not await queue.requeue(task_id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"任务 #{task_id} 无法重跑")
    return task_dict(await _get_task(services, task_id))
