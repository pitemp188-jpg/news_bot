"""
模块: newsbot.app
职责: 服务组装与生命周期——把数据库、队列、调度、流水线、网关、指令接到一起
依赖: agent.*, core.*, dispatcher.*, gateway.*, result.*
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
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
from newsbot.dispatcher.scheduler import Scheduler, cron_from_time
from newsbot.dispatcher.session import SessionStore
from newsbot.gateway.auth import Authorizer, parse_allowed
from newsbot.gateway.base import BaseAdapter, MessageEvent
from newsbot.gateway.commands import Commands
from newsbot.gateway.qqbot import QQBotAdapter
from newsbot.gateway.router import Router
from newsbot.gateway.weixin import WeixinAdapter, WeixinStore
from newsbot.result.dedup import Deduper, StoryGrouper
from newsbot.result.report import ReportBuilder

logger = get_logger(__name__)

SCHEDULED_QUERY = "请汇总「{topic}」最近一天的要点，每条附来源链接，只保留新进展。"


async def _noop() -> None:
    """调度器还没建好时的占位回调。"""
    return None


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
        self.runner: Runner | None = None

    # ── 生命周期 ──
    async def start(self) -> dict[str, bool]:
        """组装并启动全部组件，返回各平台在线状态。"""
        config = self._config
        # 先建数据目录：SQLite 不会自动创建父目录，缺目录时只会抛出
        # 一句 unable to open database file，干净机器上首次部署必踩
        config.data_dir.mkdir(parents=True, exist_ok=True)
        db = self.db = Database(config.db_url)
        await db.init()

        llm = self._llm or OpenAIClient(config.secrets)
        self._llm = llm
        sessions = SessionStore(db)
        report_builder = ReportBuilder(llm, max_chars=2000)
        runner = Runner(llm, self._build_tools(db, browser=self._browser), config.agent)
        self.runner = runner

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

        # 订阅变更后必须让调度器重新同步，否则新建的订阅要等重启才生效、
        # 退订的作业还会继续触发。回调用 lambda 延迟取 scheduler：
        # 它在这行之后才创建
        self.commands = Commands(
            db,
            queue,
            config.schedule,
            on_schedule_change=lambda: self.scheduler.sync() if self.scheduler else _noop(),
        )
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
            # 工具必须在队列停下之后、数据库之前关闭：浏览器兜底进程靠它回收，
            # 顺序反了会出现任务还在用浏览器就把它关掉、或工具拿不到数据库
            ("tools", self.runner.aclose if self.runner else None),
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
            SearchTool(
                config.agent,
                config.secrets,
                feeds=config.search.feeds,
                feed_concurrency=config.search.feed_concurrency,
            ),
            FetchTool(config.agent, allow_private=False),
            NewsDbTool(db),
        ]
        if browser is not None or self._browser_available():
            tools.append(BrowserTool(config.agent, data_dir=config.data_dir, runner=browser))
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
        if self._config.schedule.dedup_with_llm and self._llm is not None:
            # 语义分组只补词法判据的盲区（中文改写、无版本号的纯中文事件）；
            # 分组失败会退回词法判据，所以这里是纯增强，不需要额外兜错
            deduper.grouper = StoryGrouper(self._llm, timeout=self._config.schedule.dedup_llm_timeout_seconds)
        await deduper.load_recent(self.db, days)
        return deduper

    # ── 运维 ──
    async def backup(self, out: str | None = None) -> Path:
        """用 SQLite 的 VACUUM INTO 生成一致性快照，WAL 写入中也安全。"""
        import sqlite3
        from datetime import datetime

        config = self._config
        target_dir = Path(out) if out else config.data_dir / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = config.data_dir / "newsbot.db"
        if not source.exists():
            raise FileNotFoundError(f"数据库不存在：{source}")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target_dir / f"newsbot-{stamp}.db"
        # 走同步驱动：备份是短时操作，用 sqlite3 最直接，也避免和异步连接池抢锁
        with sqlite3.connect(source) as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
        logger.info("数据库快照已生成: %s", target)
        return target


def build_app(config: Config, **kwargs: Any) -> App:
    """按配置组装一个（尚未启动的）应用；测试与 CLI 共用同一入口。"""
    return App(config, **kwargs)


async def run_service() -> int:
    """CLI 入口：加载配置、组装并常驻运行。"""
    from newsbot.core.config import get_config
    from newsbot.core.log import setup_logging

    config = get_config()
    # 必须在组装 App 之前配置日志，而且不能省。Agent 首次用到浏览器时会构造
    # browser-use 的 BrowserSession，那一侧的 setup_logging 发现根日志"已有 handler"
    # 会原样保留，否则就会把根 handler 清空、换成它自己的格式。原来这里没调用：
    # 于是长跑模式（run）既没有 data/logs/newsbot.log 落盘，也没有凭证脱敏，
    # 日志格式全被第三方接管——挂机一整天出问题时无从排查。
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    config.ensure_ready()
    app = App(config)
    try:
        await app.serve()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    return 0
