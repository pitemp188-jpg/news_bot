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


async def test_subscribe_accepts_multiword_topic(harness: Harness) -> None:
    """主题可以带空格——「github 热榜」是一个主题，不能把第二个词当成时间。

    实测缺陷：原实现取 `parts[1]` 当时间，于是 /订阅 github 热榜 会把「热榜」当
    时间并报 "时间格式应为 HH:MM"，用户想安排"每晚给我 github 热榜"根本做不到。
    """
    reply = await harness.send("/订阅 github 热榜")
    assert reply is not None and "github 热榜" in reply
    rows = await harness.schedules()
    assert [(row.cron, row.topics) for row in rows] == [("0 21 * * *", ["github 热榜"])]


async def test_subscribe_accepts_hour_only_and_trailing_time(harness: Harness) -> None:
    hour_only = await harness.send("/订阅 github热榜 9")
    assert hour_only is not None and "每天 09:00" in hour_only
    trailing = await harness.send("/订阅 具身智能 融资 22:45")
    assert trailing is not None and "每天 22:45" in trailing
    rows = await harness.schedules()
    assert [(row.cron, row.topics) for row in rows] == [
        ("0 9 * * *", ["github热榜"]),
        ("45 22 * * *", ["具身智能 融资"]),
    ]


async def test_subscribe_accepts_multiple_topics(harness: Harness) -> None:
    """多个主题用逗号分隔：每个主题各自成一条检索。"""
    reply = await harness.send("/订阅 AI,芯片 20:00")
    assert reply is not None and "AI、芯片" in reply
    rows = await harness.schedules()
    assert rows[0].topics == ["AI", "芯片"]


async def test_subscribe_lists_existing(harness: Harness) -> None:
    """`/订阅` 无参数要列出已有订阅——用户需要确认"我已经安排过什么"。"""
    empty = await harness.send("/订阅")
    assert empty is not None and "没有定时推送" in empty

    await harness.send("/订阅 github 热榜 22:00")
    await harness.send("/订阅 AI 的热点")
    listing = await harness.send("/订阅")
    assert listing is not None
    assert "1. github 热榜（每天 22:00）" in listing
    assert "2. AI 的热点（每天 21:00）" in listing


async def test_subscribe_notifies_scheduler(harness: Harness) -> None:
    """订阅变更必须通知调度器，否则新订阅要到重启才生效（回归用例）。"""
    synced = []

    async def on_change() -> None:
        synced.append(True)

    harness.commands._on_schedule_change = on_change  # type: ignore[attr-defined]
    await harness.send("/订阅 半导体")
    assert synced == [True]

    # 退订同样要通知：否则作业留在调度器里继续触发
    await harness.send("/退订 1")
    assert len(synced) == 2


async def test_subscribe_rejects_bad_time(harness: Harness) -> None:
    reply = await harness.send("/订阅 半导体 25:61")
    assert reply is not None and "HH:MM" in reply
    assert await harness.schedules() == []


async def test_subscribe_rejects_hour_out_of_range(harness: Harness) -> None:
    """纯数字的末尾也当时间看，不能静默变成主题「半导体 99」。"""
    reply = await harness.send("/订阅 半导体 99")
    assert reply is not None and "HH:MM" in reply
    assert await harness.schedules() == []


async def test_subscribe_with_only_a_number_keeps_it_as_topic(harness: Harness) -> None:
    """只给了一个数字、没有主题时，不能变成"没有主题的订阅"。"""
    reply = await harness.send("/订阅 9")
    assert reply is not None and "9" in reply
    rows = await harness.schedules()
    assert [(row.cron, row.topics) for row in rows] == [("0 21 * * *", ["9"])]


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
