"""
模块: gateway.commands
职责: 聊天指令——帮助、搜索、订阅、退订、任务列表与取消
依赖: core.db, core.models, dispatcher.queue, dispatcher.session, gateway.base
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from newsbot.core.config import ScheduleSection
from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Schedule, Task
from newsbot.dispatcher.queue import TaskQueue
from newsbot.dispatcher.scheduler import cron_from_time
from newsbot.gateway.base import MessageEvent, safe_id

logger = get_logger(__name__)

HELP_TEXT = """可用指令：
直接发文字或 /搜 <内容>：立即搜索并把结果发给你
/订阅 查看已有定时推送
/订阅 <主题> [HH:MM]：每天定时推送该主题（默认 21:00）
　主题可以带空格，如「github 热榜」；多个主题用逗号分隔，如「AI,芯片」
　时间也可以只写小时，如「/订阅 github热榜 9」＝ 每天 09:00
/退订 <编号>：删除一条定时推送
/任务：查看最近任务
/停止 [编号]：取消正在执行的任务
/帮助：显示本说明"""

# 主题与时间的分隔：主题可以是多个词，时间写在末尾。这里只做词法切分，
# 判断"哪个词是时间"交给 `_split_topic_time`（它复用调度器的 HH:MM 校验）
_SPLIT_HINTS = (" ", "，", ",", "、")
_TOPIC_SPLIT = re.compile(r"[，,、]+")
# 允许只写小时（"9" → 09:00），但拒绝 0 与大于 23 的值——它们更可能是数字而非时间
_HOUR_ONLY = re.compile(r"^([01]?\d|2[0-3])$")
# `HH:MM` 的外形（不校验范围）：用来判断"用户是在写时间"
_CLOCK_SHAPE = re.compile(r"^\d{1,2}:\d{2}$")

MAX_LISTED_SCHEDULES = 10
MAX_LISTED_TASKS = 5
ACTIVE_STATES = ("pending", "running")

_CRON_TO_TIME = re.compile(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+\*$")


def _render_schedules(rows: list[Schedule]) -> str:
    """把订阅渲染成"编号. 主题（每天 HH:MM）"，编号与 /退订 的序号一致。"""
    lines = []
    for index, row in enumerate(rows[:MAX_LISTED_SCHEDULES], 1):
        topics = "、".join(str(topic) for topic in (row.topics or [])) or "(无主题)"
        lines.append(f"{index}. {topics}（每天 {_cron_to_time(row.cron)}）")
    if len(rows) > MAX_LISTED_SCHEDULES:
        lines.append(f"（另有 {len(rows) - MAX_LISTED_SCHEDULES} 条未列出）")
    return "\n".join(lines)


def _cron_to_time(cron: str) -> str:
    """cron 还原成 HH:MM；不是"每天某点"的形式就原样返回。"""
    match = _CRON_TO_TIME.match((cron or "").strip())
    if match is None:
        return cron or "未知时间"
    return f"{int(match.group(2)):02d}:{int(match.group(1)):02d}"


def _split_topic_time(args: str, default_time: str) -> tuple[str, str]:
    """把参数拆成（主题, 时间）；末尾像时间却非法时抛 ValueError。

    时间写在**末尾**才算时间：主题经常带空格（「github 热榜」），而用户更可能漏写
    时间（用默认）而不是漏写主题。只写小时也接受（「9」＝ 09:00）。

    判断"像时间"不能只看 cron 校验是否通过——那样「/订阅 半导体 25:61」会被当成
    主题「半导体 25:61」静默接受，用户不知道时间写错了。这里的规则是：末尾是
    `HH:MM` 形式或纯数字时，它**意图是时间**，不合法就报错；否则当主题的一部分。
    """
    parts = [part for part in re.split(r"\s+", (args or "").strip()) if part]
    if not parts:
        return "", default_time
    last = parts[-1]
    looks_like_time = bool(_CLOCK_SHAPE.match(last)) or last.isdigit()
    if looks_like_time:
        try:
            cron_from_time(last if ":" in last else f"{last}:00")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        rest = " ".join(parts[:-1])
        if not rest:
            # 只给了时间、没给主题：回退成默认时间并把整个参数当主题，
            # 否则「/订阅 9」会变成没有主题的订阅
            return " ".join(parts), default_time
        return rest, f"{int(last):02d}:00" if last.isdigit() else last
    return " ".join(parts), default_time


class Commands:
    """解析并执行聊天指令；返回需要立刻回复的文本（长任务由队列异步执行）。

    `on_schedule_change` 是订阅变更后的回调（实际接 `Scheduler.sync`）。**必须有它**：
    调度器的作业集合来自数据库，只写库不 sync，新建的订阅要到重启才生效、退订的
    作业则继续照常触发——用户看到的就是"安排了任务却收不到"或"退了还在推"。
    """

    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        settings: ScheduleSection,
        *,
        on_schedule_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._queue = queue
        self._settings = settings
        self._on_schedule_change = on_schedule_change

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
        """新增一条定时推送。

        主题允许带空格（「github 热榜」），所以时间必须从**末尾**识别，不能像以前
        那样把第二个词直接当成时间——那会让「/订阅 github 热榜」把「热榜」当时间
        并报错。无参数时列出已有订阅，方便用户确认"我已经安排过什么"。
        """
        existing = await self._schedules(event)
        if not args:
            if not existing:
                return "当前没有定时推送。\n\n用法：/订阅 <主题> [HH:MM]\n例：/订阅 github 热榜 每晚时间默认为 21:00"
            return f"当前定时推送：\n{_render_schedules(existing)}\n\n用法：/订阅 <主题> [HH:MM]"

        try:
            topic_text, time_text = _split_topic_time(args, self._settings.default_time)
            cron = cron_from_time(time_text)
        except ValueError as exc:
            return str(exc)
        if not topic_text:
            return "请给出要推送的主题，例如 /订阅 github 热榜 22:00"
        topics = [part.strip() for part in _TOPIC_SPLIT.split(topic_text) if part.strip()][:3]
        if not topics:
            return "请给出要推送的主题，例如 /订阅 github 热榜 22:00"
        async with self._db.session() as session:
            session.add(Schedule(cron=cron, topics=topics, platform=event.platform, chat_id=event.chat_id))
            await session.commit()
        await self._sync_schedules()
        shown = "、".join(topics)
        return f"已订阅「{shown}」，每天 {time_text} 推送。\n查看全部：/订阅；取消：/退订 <编号>"

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
        # 不同步的话作业还在调度器里，退订之后照常触发
        await self._sync_schedules()
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

    async def _sync_schedules(self) -> None:
        if self._on_schedule_change is None:
            return
        try:
            await self._on_schedule_change()
        except Exception as exc:  # 同步失败不该吞掉已成功的订阅操作
            logger.warning("同步定时作业失败: %s", exc)
