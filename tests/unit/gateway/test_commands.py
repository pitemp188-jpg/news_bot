"""
模块: tests.unit.gateway.test_commands
职责: 校验聊天指令解析——搜索、帮助、订阅、退订、任务列表与取消
依赖: gateway.commands, dispatcher.queue
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from newsbot.core.config import QueueSection, ScheduleSection
from newsbot.core.db import Database
from newsbot.core.models import Schedule, Task
from newsbot.dispatcher.queue import TaskQueue
from newsbot.gateway.base import MessageEvent
from newsbot.gateway.commands import Commands, cron_from_time


async def _executor(_session: object, _task: Task) -> str | None:
    return None


class Harness:
    """把命令、队列与数据库装到一起，便于逐个指令断言。"""

    def __init__(self, db: Database, queue: TaskQueue, commands: Commands) -> None:
        self.db = db
        self.queue = queue
        self.commands = commands

    async def send(self, text: str, *, platform: str = "fake", chat_id: str = "c1") -> str | None:
        event = MessageEvent(platform=platform, chat_id=chat_id, chat_type="dm", user_id=chat_id, text=text)
        return await self.commands.handle(event)

    async def schedules(self) -> list[Schedule]:
        async with self.db.session() as session:
            return list((await session.execute(select(Schedule))).scalars())

    async def tasks(self) -> list[Task]:
        async with self.db.session() as session:
            return list((await session.execute(select(Task))).scalars())


@pytest.fixture
async def harness(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'commands.db'}")
    await db.init()
    queue = TaskQueue(db, QueueSection(concurrency=1, task_timeout_seconds=10), _executor)
    commands = Commands(db, queue, ScheduleSection(default_time="21:00", default_topics=["AI"]))
    try:
        yield Harness(db, queue, commands)
    finally:
        await db.dispose()


def test_cron_from_time() -> None:
    assert cron_from_time("21:00") == "0 21 * * *"
    assert cron_from_time("7:05") == "5 7 * * *"
    with pytest.raises(ValueError):
        cron_from_time("25:00")
    with pytest.raises(ValueError):
        cron_from_time("21:60")
    with pytest.raises(ValueError):
        cron_from_time("晚上九点")


async def test_plain_text_becomes_search_task(harness: Harness) -> None:
    reply = await harness.send("  最近 AI 有什么新进展  ")
    assert reply is not None and "已收到" in reply
    tasks = await harness.tasks()
    assert [(task.kind, task.query, task.platform, task.chat_id) for task in tasks] == [
        ("chat", "最近 AI 有什么新进展", "fake", "c1")
    ]


async def test_help_and_unknown(harness: Harness) -> None:
    assert "可用指令" in (await harness.send("/帮助") or "")
    assert "可用指令" in (await harness.send("/help") or "")
    unknown = await harness.send("/转发 你好")
    assert unknown is not None and "未知指令" in unknown


async def test_search_command_requires_argument(harness: Harness) -> None:
    assert "用法" in (await harness.send("/搜") or "")
    await harness.send("/搜 芯片 出口管制")
    tasks = await harness.tasks()
    assert [task.query for task in tasks] == ["芯片 出口管制"]


async def test_subscribe_uses_default_and_explicit_time(harness: Harness) -> None:
    default = await harness.send("/订阅 半导体")
    assert default is not None and "每天 21:00" in default
    explicit = await harness.send("/订阅 新能源 08:30")
    assert explicit is not None and "每天 08:30" in explicit

    rows = await harness.schedules()
    assert [(row.cron, row.topics) for row in rows] == [("0 21 * * *", ["半导体"]), ("30 8 * * *", ["新能源"])]
    assert all(row.platform == "fake" and row.chat_id == "c1" and row.enabled for row in rows)


async def test_subscribe_rejects_bad_time(harness: Harness) -> None:
    reply = await harness.send("/订阅 半导体 25:61")
    assert reply is not None and "HH:MM" in reply
    assert await harness.schedules() == []


async def test_unsubscribe_lists_and_disables(harness: Harness) -> None:
    await harness.send("/订阅 半导体")
    await harness.send("/订阅 新能源")
    listing = await harness.send("/退订")
    assert listing is not None and "1. 半导体" in listing and "2. 新能源" in listing

    assert "编号应为数字" in (await harness.send("/退订 abc") or "")
    assert "超出范围" in (await harness.send("/退订 9") or "")

    done = await harness.send("/退订 1")
    assert done is not None and "已退订「半导体」" in done
    rows = await harness.schedules()
    assert [(row.topics[0], row.enabled) for row in rows] == [("半导体", False), ("新能源", True)]


async def test_unsubscribe_without_schedules(harness: Harness) -> None:
    assert (await harness.send("/退订 1")) == "当前没有定时推送。"


async def test_task_list_and_cancel(harness: Harness) -> None:
    assert (await harness.send("/任务")) == "还没有任务记录。"
    await harness.send("/搜 一号话题")
    await harness.send("/搜 二号话题")

    listing = await harness.send("/任务")
    assert listing is not None and "#2" in listing and "二号话题" in listing

    await harness.queue.submit(kind="manual", query="待取消", platform="fake", chat_id="c1")
    cancelled = await harness.send("/停止")
    assert cancelled is not None and "已取消任务 #3" in cancelled

    assert "找到可取消" in (await harness.send("/停止 99") or "")
    assert "编号应为数字" in (await harness.send("/停止 x") or "")


async def test_empty_text_is_ignored(harness: Harness) -> None:
    assert (await harness.send("   ")) is None
