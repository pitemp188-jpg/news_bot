"""
模块: newsbot.app
职责: 服务组装与生命周期——把数据库、队列、调度、流水线、网关、指令接到一起
依赖: agent.*, core.*, dispatcher.*, gateway.*, result.*
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from newsbot.agent.runner import Runner
from newsbot.agent.tools.browser import BrowserRunner, BrowserTool
from newsbot.agent.tools.fetch import FetchTool
from newsbot.agent.tools.newsdb import NewsDbTool
from newsbot.agent.tools.search import SearchTool
from newsbot.core.config import Config
from newsbot.core.db import Database
from newsbot.core.llm import LLM, OpenAIClient
from newsbot.core.log import get_logger
from newsbot.core.models import Schedule
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.queue import TaskQueue
from newsbot.dispatcher.scheduler import Scheduler
from newsbot.dispatcher.session import SessionStore
from newsbot.gateway.auth import Authorizer, parse_allowed
from newsbot.gateway.base import BaseAdapter, MessageEvent
from newsbot.gateway.commands import Commands, cron_from_time
from newsbot.gateway.qqbot import QQBotAdapter
from newsbot.gateway.router import Router
from newsbot.gateway.weixin import WeixinAdapter, WeixinStore
from newsbot.result.dedup import Deduper
from newsbot.result.report import ReportBuilder

logger = get_logger(__name__)

SCHEDULED_QUERY = "请汇总「{topic}」最近一天的要点，每条附来源链接，只保留新进展。"


@dataclass
class Target:
    """投递目标。"""

    platform: str
    chat_id: str


class App:
    """单进程应用：一处组装全部组件，便于测试时注入替身。"""

    def __init__(
        self,
        config: Config,
        *,
        llm: LLM | None = None,
        adapters: list[BaseAdapter] | None = None,
        tools: list[Any] | None = None,
        browser: BrowserRunner | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._adapters_in = adapters
        self._tools_in = tools
        self._browser = browser
        self.db: Database | None = None
        self.queue: TaskQueue | None = None
        self.router: Router | None = None
        self.scheduler: Scheduler | None = None
        self.commands: Commands | None = None

    # ── 生命周期 ──
    async def start(self) -> dict[str, bool]:
        """组装并启动全部组件，返回各平台在线状态。"""
        config = self._config
        db = self.db = Database(config.db_url)
        await db.init()

        llm = self._llm or OpenAIClient(config.secrets)
        self._llm = llm
        sessions = SessionStore(db)
        report_builder = ReportBuilder(llm, max_chars=2000)
        runner = Runner(llm, self._build_tools(db, browser=self._browser), config.agent)

        authorizer = Authorizer(
            {
                "weixin": parse_allowed(config.secrets.weixin_allowed_users),
                "qqbot": parse_allowed(config.secrets.qq_allowed_users),
            },
            root=config.data_dir,
        )
        router = self.router = Router(db, authorizer, config.delivery)
        pipeline = Pipeline(
            db,
            runner=runner,
            reporter=report_builder,
            sender=router,
            sessions=sessions,
            deduper=self._make_deduper,
        )
        queue = self.queue = TaskQueue(db, config.queue, pipeline.execute)
        await queue.start()

        self.commands = Commands(db, queue, config.schedule)
        router.set_inbound(self._on_message)

        scheduler = self.scheduler = Scheduler(db, timezone=config.app.timezone, submit=self._submit_schedule)
        await scheduler.sync()
        scheduler.start()

        for adapter in self._adapters_in if self._adapters_in is not None else self._platform_adapters():
            router.register(adapter)
        online = await router.start()
        logger.info("服务已启动，平台状态: %s", online)
        return online

    async def stop(self) -> None:
        """按依赖倒序关闭，任何一步失败都不阻断其余清理。"""
        for name, action in (
            ("router", self.router.stop if self.router else None),
            ("scheduler", self.scheduler.shutdown if self.scheduler else None),
            ("queue", self.queue.stop if self.queue else None),
            ("llm", getattr(self._llm, "aclose", None)),
            ("db", self.db.dispose if self.db else None),
        ):
            if action is None:
                continue
            try:
                await action()
            except Exception as exc:
                logger.warning("关闭 %s 失败: %s", name, exc)

    async def serve(self) -> None:
        """启动后常驻，直到收到 KeyboardInterrupt 或被取消。"""
        await self.start()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    # ── 入站与定时 ──
    async def _on_message(self, event: MessageEvent) -> None:
        created = await self._ensure_default_schedule(event)
        commands = self.commands
        router = self.router
        if commands is None or router is None:
            return
        reply = await commands.handle(event)
        if reply and created is not None:
            reply = f"{reply}\n\n已默认开启每日 {self._config.schedule.default_time} 的推送（用 /退订 1 关闭）。"
        if reply:
            await router.send(platform=event.platform, chat_id=event.chat_id, text=reply)

    async def _ensure_default_schedule(self, event: MessageEvent) -> Schedule | None:
        """首次对话的用户自动获得一条默认定时推送（每天 21:00）。"""
        assert self.db is not None
        async with self.db.session() as session:
            existing = (
                await session.execute(
                    select(Schedule.id).where(
                        Schedule.platform == event.platform,
                        Schedule.chat_id == event.chat_id,
                    )
                )
            ).first()
            if existing is not None:
                return None
            topics = list(self._config.schedule.default_topics) or ["AI"]
            row = Schedule(
                cron=cron_from_time(self._config.schedule.default_time),
                topics=topics,
                platform=event.platform,
                chat_id=event.chat_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        logger.info("为 %s/%s 建立默认订阅: %s", event.platform, event.chat_id, topics)
        if self.scheduler is not None:
            await self.scheduler.sync()
        return row

    async def _submit_schedule(self, row: Schedule) -> None:
        """定时触发：把订阅转成任务。"""
        assert self.queue is not None
        topics = [str(topic) for topic in (row.topics or [])] or ["AI"]
        query = "；".join(SCHEDULED_QUERY.format(topic=topic) for topic in topics[:3])
        await self.queue.submit(kind="scheduled", query=query, platform=row.platform, chat_id=row.chat_id)

    # ── 主动推送（notify） ──
    async def notify(self, text: str, *, platform: str | None = None, chat_id: str | None = None) -> bool:
        """推送一条文本；未指定目标时发给所有已订阅会话。"""
        router = self.router
        if router is None:
            raise RuntimeError("服务未启动，无法推送")
        targets = [Target(platform, chat_id)] if platform and chat_id else await self.targets()
        if not targets:
            logger.warning("没有可用的推送目标，请先给机器人发一条消息")
            return False
        results = [await router.send(platform=target.platform, chat_id=target.chat_id, text=text) for target in targets]
        return any(results)

    async def targets(self) -> list[Target]:
        """从订阅表推导投递目标。"""
        assert self.db is not None
        async with self.db.session() as session:
            rows = (await session.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars().all()
        seen: dict[tuple[str, str], Target] = {}
        for row in rows:
            seen.setdefault((row.platform, row.chat_id), Target(row.platform, row.chat_id))
        return list(seen.values())

    # ── 组装细节 ──
    def _build_tools(self, db: Database, *, browser: BrowserRunner | None) -> list[Any]:
        if self._tools_in is not None:
            return list(self._tools_in)
        config = self._config
        tools: list[Any] = [
            SearchTool(config.agent, config.secrets),
            FetchTool(config.agent, allow_private=False),
            NewsDbTool(db),
        ]
        if browser is not None or self._browser_available():
            tools.append(BrowserTool(config.agent, runner=browser))
        return tools

    @staticmethod
    def _browser_available() -> bool:
        try:
            import browser_use  # noqa: F401
        except ImportError:
            return False
        return True

    def _platform_adapters(self) -> list[BaseAdapter]:
        """按已配置的凭证决定启用哪些平台。"""
        config = self._config
        adapters: list[BaseAdapter] = []
        if config.secrets.qq_app_id and config.secrets.qq_client_secret:
            adapters.append(QQBotAdapter(config.secrets))
        store = WeixinStore(config.data_dir)
        if config.secrets.weixin_token or store.load_account() is not None:
            adapters.append(WeixinAdapter(config.secrets, store))
        if not adapters:
            logger.warning("未配置任何平台凭证，仅运行定时与本地任务")
        return adapters

    async def _make_deduper(self) -> Deduper | None:
        """每次任务载入近 N 天指纹；去重开关由 dedup_days <= 0 关闭。"""
        days = self._config.schedule.dedup_days
        if days <= 0 or self.db is None:
            return None
        deduper = Deduper()
        await deduper.load_recent(self.db, days)
        return deduper


async def run_service() -> int:
    """CLI 入口：加载配置、组装并常驻运行。"""
    from newsbot.core.config import get_config

    config = get_config()
    config.ensure_ready()
    app = App(config)
    try:
        await app.serve()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    return 0
