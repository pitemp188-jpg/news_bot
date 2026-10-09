"""
模块: api.routes.system
职责: 系统接口——运行状态（适配器/队列/任务统计/今日用量）与主动推送
依赖: api.deps, core.models, dispatcher.queue, gateway.router
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from newsbot.api.deps import Authed, Services, get_services
from newsbot.core.models import Delivery, LlmUsage, NewsItem, Report, Schedule, Task

router = APIRouter(prefix="/system", tags=["system"], dependencies=[Authed])

# 前端要按固定状态渲染卡片，缺失状态补 0 而不是让界面自己判空
TASK_STATES = ("pending", "running", "succeeded", "failed", "timeout", "cancelled")


class NotifyPayload(BaseModel):
    """主动推送一条文本。"""

    text: str = Field(min_length=1, max_length=4000)
    platform: str | None = None
    chat_id: str | None = None


@router.get("/status")
async def system_status(services: Services = Depends(get_services)) -> dict[str, Any]:
    """一眼看清服务是否健康：平台在线、队列积压、任务与内容计数、今日 token。"""
    app = services.app
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with services.db.session() as session:
        status_rows = (await session.execute(select(Task.status, func.count(Task.id)).group_by(Task.status))).all()
        counts = {state: 0 for state in TASK_STATES}
        counts.update({str(row[0]): int(row[1]) for row in status_rows})

        async def _count(model: Any, *conditions: Any) -> int:
            statement = select(func.count(model.id))
            if conditions:
                statement = statement.where(*conditions)
            return int((await session.execute(statement)).scalar_one())

        usage = (
            await session.execute(
                select(
                    func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
                ).where(LlmUsage.created_at >= day_start)
            )
        ).one()
        counts["news"] = await _count(NewsItem)
        counts["reports"] = await _count(Report)
        counts["deliveries"] = await _count(Delivery)
        counts["schedules"] = await _count(Schedule, Schedule.enabled.is_(True))
        waiting = await _count(Delivery, Delivery.status == "waiting_user")

    queue = app.queue
    router_online = app.router.online() if app.router is not None else {}
    return {
        "platforms": router_online,
        "queue": {"depth": queue.depth if queue else 0, "running": queue.running if queue else 0},
        "tasks": {state: counts[state] for state in TASK_STATES},
        "running": {"deliveries": counts["deliveries"], "waiting_user": waiting},
        "content": {"news": counts["news"], "reports": counts["reports"], "schedules": counts["schedules"]},
        "today_tokens": {"prompt": int(usage[0] or 0), "completion": int(usage[1] or 0)},
        "timezone": services.config.app.timezone,
    }


@router.post("/notify")
async def push_notify(payload: NotifyPayload, services: Services = Depends(get_services)) -> dict[str, Any]:
    """把一条文本推到指定会话；不指定则推给所有已订阅会话。"""
    if services.app.router is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "网关未就绪")
    ok = await services.app.notify(payload.text, platform=payload.platform, chat_id=payload.chat_id)
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "没有可用的推送目标，或平台拒绝接收")
    return {"ok": True}
