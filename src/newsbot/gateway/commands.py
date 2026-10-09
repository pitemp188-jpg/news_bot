"""
模块: gateway.commands
职责: 聊天指令——帮助、搜索、订阅、退订、任务列表与取消
依赖: core.db, core.models, dispatcher.queue, dispatcher.session, gateway.base
"""

from __future__ import annotations

import re

from sqlalchemy import select

from newsbot.core.config import ScheduleSection
from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Schedule, Task
from newsbot.dispatcher.queue import TaskQueue
from newsbot.gateway.base import MessageEvent, safe_id

logger = get_logger(__name__)

HELP_TEXT = """可用指令：
直接发文字或 /搜 <内容>：立即搜索并把结果发给你
/订阅 <主题> [HH:MM]：每天定时推送该主题（默认 21:00）
/退订 <编号>：删除一条定时推送
/任务：查看最近任务
/停止 [编号]：取消正在执行的任务
/帮助：显示本说明"""

MAX_LISTED_SCHEDULES = 10
MAX_LISTED_TASKS = 5
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ACTIVE_STATES = ("pending", "running")


def cron_from_time(value: str) -> str:
    """把 HH:MM 转成五段式 cron；非法输入回退到默认时间。"""
    match = _TIME_RE.match(value.strip())
    if match is None:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    return f"{int(match.group(2))} {int(match.group(1))} * * *"


class Commands:
    """解析并执行聊天指令；返回需要立刻回复的文本（长任务由队列异步执行）。"""

    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        settings: ScheduleSection,
    ) -> None:
        self._db = db
        self._queue = queue
        self._settings = settings

    async def handle(self, event: MessageEvent) -> str | None:
        text = (event.text or "").strip()
        if not text:
            return None
        if not text.startswith("/"):
            return await self._submit_search(event, text)

        name, _, args = text[1:].partition(" ")
        args = args.strip()
        if name in {"帮助", "help"}:
            return HELP_TEXT
        if name in {"搜", "search"}:
            if not args:
                return "用法：/搜 <要查的内容>"
            return await self._submit_search(event, args)
        if name == "订阅":
            return await self._subscribe(event, args)
        if name == "退订":
            return await self._unsubscribe(event, args)
        if name == "任务":
            return await self._list_tasks(event)
        if name == "停止":
            return await self._cancel(event, args)
        return f"未知指令 /{name}。\n\n{HELP_TEXT}"

    # ── 指令实现 ──
    async def _submit_search(self, event: MessageEvent, query: str) -> str:
        task = await self._queue.submit(kind="chat", query=query, platform=event.platform, chat_id=event.chat_id)
        logger.info("指令任务 #%d 来自 %s: %s", task.id, safe_id(event.user_id), query[:40])
        return f"已收到，正在搜索：{query}\n任务 #{task.id}，完成后把结果发给你。"

    async def _subscribe(self, event: MessageEvent, args: str) -> str:
        if not args:
            return "用法：/订阅 <主题> [HH:MM]"
        parts = args.split()
        topic, time_text = parts[0], (parts[1] if len(parts) > 1 else self._settings.default_time)
        try:
            cron = cron_from_time(time_text)
        except ValueError as exc:
            return str(exc)
        async with self._db.session() as session:
            session.add(Schedule(cron=cron, topics=[topic], platform=event.platform, chat_id=event.chat_id))
            await session.commit()
        return f"已订阅「{topic}」，每天 {time_text} 推送。"

    async def _unsubscribe(self, event: MessageEvent, args: str) -> str:
        rows = await self._schedules(event)
        if not rows:
            return "当前没有定时推送。"
        if not args:
            listing = "\n".join(f"{index}. {row.topics[0]}" for index, row in enumerate(rows, 1))
            return f"用法：/退订 <编号>\n当前订阅：\n{listing}"
        try:
            index = int(args.split()[0])
        except ValueError:
            return "编号应为数字，例如 /退订 1"
        if not 1 <= index <= len(rows):
            return f"编号超出范围，当前共 {len(rows)} 条订阅。"
        target = rows[index - 1]
        async with self._db.session() as session:
            row = await session.get(Schedule, target.id)
            if row is not None:
                row.enabled = False
            await session.commit()
        return f"已退订「{target.topics[0] if target.topics else target.id}」。"

    async def _list_tasks(self, event: MessageEvent) -> str:
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.chat_id == event.chat_id, Task.platform == event.platform)
                        .order_by(Task.id.desc())
                        .limit(MAX_LISTED_TASKS)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return "还没有任务记录。"
        lines = [f"#{row.id} [{row.status}] {row.query[:30]}" for row in rows]
        return "最近任务：\n" + "\n".join(lines)

    async def _cancel(self, event: MessageEvent, args: str) -> str:
        task = None
        if args:
            try:
                task = await self._queue.find(int(args.split()[0]))
            except ValueError:
                return "编号应为数字，例如 /停止 3"
        else:
            async with self._db.session() as session:
                task = (
                    await session.execute(
                        select(Task)
                        .where(Task.chat_id == event.chat_id, Task.status.in_(ACTIVE_STATES))
                        .order_by(Task.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        if task is None:
            return "没有找到可取消的任务。"
        cancelled = await self._queue.cancel(task.id)
        return f"已取消任务 #{task.id}。" if cancelled else f"任务 #{task.id} 已结束，无需取消。"

    async def _schedules(self, event: MessageEvent) -> list[Schedule]:
        async with self._db.session() as session:
            return list(
                (
                    await session.execute(
                        select(Schedule)
                        .where(
                            Schedule.platform == event.platform,
                            Schedule.chat_id == event.chat_id,
                            Schedule.enabled.is_(True),
                        )
                        .order_by(Schedule.id)
                        .limit(MAX_LISTED_SCHEDULES)
                    )
                )
                .scalars()
                .all()
            )
