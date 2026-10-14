"""
模块: dispatcher.scheduler
职责: 定时调度——cron 解析、从 schedule 表同步作业、到点把订阅转成任务提交
依赖: core.db, core.log, core.models
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Schedule

logger = get_logger(__name__)

SubmitSchedule = Callable[[Schedule], Awaitable[None]]
RefreshEvents = Callable[[], Awaitable[None]]
CollectFeeds = Callable[[], Awaitable[None]]

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# 事件层刷新作业的固定 id：它不属于任何订阅，用一个不会与 `schedule-<id>` 冲突的名字
REFRESH_JOB_ID = "events-refresh"
# 定时采集作业的固定 id。它把订阅源的最新条目拉进资讯库——没有它，库里只在有人
# 提问或订阅触发时才进料（实测某天 0 条），事件层刷新永远看到「候选 0 条」
COLLECT_JOB_ID = "collect-feeds"


def cron_from_time(value: str) -> str:
    """把 HH:MM 转成五段式 cron；非法输入抛 ValueError。"""
    match = _TIME_RE.match((value or "").strip())
    if match is None:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    return f"{int(match.group(2))} {int(match.group(1))} * * *"


def job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


class Scheduler:
    """APScheduler 薄封装：作业内容由 submit 注入，便于单独测试。"""

    def __init__(
        self,
        db: Database,
        *,
        timezone: str,
        submit: SubmitSchedule,
        refresh: RefreshEvents | None = None,
        refresh_interval: int | None = None,
        collect: CollectFeeds | None = None,
        collect_interval: int | None = None,
    ) -> None:
        self._db = db
        self._submit = submit
        self._refresh = refresh
        self._refresh_interval = refresh_interval or 0
        self._collect = collect
        self._collect_interval = collect_interval or 0
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
        """让作业集合与数据库中的启用订阅保持一致，并挂上事件层刷新。"""
        async with self._db.session() as session:
            rows = (await session.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars().all()

        expected = {job_id(row.id) for row in rows}
        if self._refresh is not None and self._refresh_interval > 0:
            expected.add(REFRESH_JOB_ID)
        if self._collect is not None and self._collect_interval > 0:
            expected.add(COLLECT_JOB_ID)
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
        if self._refresh is not None and self._refresh_interval > 0:
            # 事件层按间隔刷新而非固定时刻：抓取随时在写库，聚簇要能跟上新到货
            self._scheduler.add_job(
                self.run_refresh,
                trigger=IntervalTrigger(minutes=self._refresh_interval),
                id=REFRESH_JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        if self._collect is not None and self._collect_interval > 0:
            # 采集与刷新是两个独立作业（进料 / 算榜），不是一个链：混在一起后
            # 「刷新失败」与「采集失败」在日志里分不出来。代价是新一轮数据最晚要
            # 等下一次刷新才被聚簇（≤ 一个间隔），这个延迟不值得用可观测性换。
            #
            # `next_run_time` 让它在启动时立刻跑一次：否则服务重启后要干等一个
            # 间隔才有第一批数据进来，而重启恰恰是「刚部署完想看效果」的时刻
            self._scheduler.add_job(
                self.run_collect,
                trigger=IntervalTrigger(minutes=self._collect_interval),
                id=COLLECT_JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(self._scheduler.timezone) + timedelta(seconds=5),
            )
        logger.info("定时作业已同步，共 %d 个", len(self.job_ids()))

    async def run_refresh(self) -> None:
        """事件层刷新：聚簇 → 精选 → 热度快照。失败只记日志，不能拖垮调度器。"""
        if self._refresh is None:
            return
        try:
            await self._refresh()
        except Exception:
            logger.exception("事件层刷新失败")

    async def run_collect(self) -> None:
        """定时采集：拉订阅源最新条目入库。失败只记日志，不能拖垮调度器。"""
        if self._collect is None:
            return
        try:
            await self._collect()
        except Exception:
            logger.exception("定时采集失败")

    async def run_job(self, schedule_id: int) -> None:
        """作业体：订阅仍启用时提交任务。"""
        async with self._db.session() as session:
            row = await session.get(Schedule, schedule_id)
            if row is None or not row.enabled:
                logger.warning("订阅 #%d 已失效，跳过本次触发", schedule_id)
                return
            await self._submit(row)
