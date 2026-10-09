"""
模块: dispatcher.scheduler
职责: 定时调度——从 schedule 表同步作业，到点把订阅转成任务提交
依赖: core.db, core.log, core.models
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Schedule

logger = get_logger(__name__)

SubmitSchedule = Callable[[Schedule], Awaitable[None]]


def job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


class Scheduler:
    """APScheduler 薄封装：作业内容由 submit 注入，便于单独测试。"""

    def __init__(self, db: Database, *, timezone: str, submit: SubmitSchedule) -> None:
        self._db = db
        self._submit = submit
        self._scheduler = AsyncIOScheduler(timezone=timezone)

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            # AsyncIOScheduler 的停止回调排在事件循环上，让出一拍等它落地
            await asyncio.sleep(0)

    def job_ids(self) -> set[str]:
        return {job.id for job in self._scheduler.get_jobs()}

    async def sync(self) -> None:
        """让作业集合与数据库中的启用订阅保持一致。"""
        async with self._db.session() as session:
            rows = (await session.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars().all()

        expected = {job_id(row.id) for row in rows}
        for stale in self.job_ids() - expected:
            self._scheduler.remove_job(stale)
        for row in rows:
            self._scheduler.add_job(
                self.run_job,
                trigger=CronTrigger.from_crontab(row.cron, timezone=self._scheduler.timezone),
                id=job_id(row.id),
                args=[row.id],
                replace_existing=True,
            )
        logger.info("定时作业已同步，共 %d 个", len(expected))

    async def run_job(self, schedule_id: int) -> None:
        """作业体：订阅仍启用时提交任务。"""
        async with self._db.session() as session:
            row = await session.get(Schedule, schedule_id)
            if row is None or not row.enabled:
                logger.warning("订阅 #%d 已失效，跳过本次触发", schedule_id)
                return
            await self._submit(row)
