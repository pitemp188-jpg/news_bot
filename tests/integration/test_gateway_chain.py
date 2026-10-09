"""
模块: tests.integration.test_gateway_chain
职责: 集成校验——真实路由 + 真实队列 + 真实 Agent 抓取本地站点，从入站消息到出站投递
依赖: gateway.router, dispatcher.queue, agent.runner
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.runner import Runner
from newsbot.agent.tools.fetch import FetchTool
from newsbot.core.config import AgentSection, DeliverySection, QueueSection
from newsbot.core.db import Database
from newsbot.core.llm import ToolCall
from newsbot.core.models import Delivery, Task
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.queue import TaskQueue
from newsbot.dispatcher.session import SessionStore
from newsbot.gateway.auth import Authorizer
from newsbot.gateway.base import MessageEvent, SendResult
from newsbot.gateway.router import Router
from newsbot.result.report import ReportBuilder

pytestmark = pytest.mark.integration


@dataclass
class FlakyAdapter(FakeAdapter):
    """第一次发送报“会话未就绪”，之后正常——模拟平台要求用户先发消息。"""

    platform: str = "weixin"
    blocked_once: bool = True

    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        self.sent.append((chat_id, text, reply_to))
        if self.blocked_once:
            self.blocked_once = False
            return SendResult.failure("iLink 会话未就绪", need_user=True)
        return SendResult.success(f"fake-{len(self.sent)}")


async def _wait(db: Database, task_id: int, expected: str, timeout: float = 15.0) -> str:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = ""
    while loop.time() < deadline:
        async with db.session() as session:
            task = await session.get(Task, task_id)
            status = task.status if task else ""
        if status == expected:
            return status
        await asyncio.sleep(0.02)
    return status


async def test_inbound_message_to_agent_to_delivery(tmp_path: Path, local_server: str) -> None:
    url = f"{local_server}/article.html"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'gateway-chain.db'}")
    await db.init()

    runner = Runner(
        FakeLLM(
            replies=[
                reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI"})]),
                reply("", calls=[ToolCall(id="c2", name="fetch", arguments={"url": url})]),
                reply("推理成本较上代下降约三成 [1]"),
            ]
        ),
        [
            FakeSearchTool(hits=[{"title": "示例资讯：AI 行业周报", "url": url, "snippet": "摘要"}]),
            FetchTool(AgentSection(), allow_private=True),
        ],
        AgentSection(max_steps=6, max_tokens=10_000),
    )

    submitted: list[str] = []
    queue_holder: dict[str, TaskQueue] = {}

    async def inbound(event: MessageEvent) -> None:
        submitted.append(event.text)
        await queue_holder["queue"].submit(
            kind="chat", query=event.text, platform=event.platform, chat_id=event.chat_id
        )

    async def instant(_seconds: float) -> None:
        return None

    router = Router(db, Authorizer({"weixin": {"u1"}}), DeliverySection(max_attempts=2), sleeper=instant)
    router.set_inbound(inbound)
    adapter = FakeAdapter(platform="weixin")
    router.register(adapter)

    sessions = SessionStore(db)
    pipeline = Pipeline(
        db,
        runner=runner,
        reporter=ReportBuilder(),
        sender=router,
        sessions=sessions,
    )
    queue = TaskQueue(db, QueueSection(concurrency=1, task_timeout_seconds=30), pipeline.execute)
    queue_holder["queue"] = queue
    await queue.start()
    try:
        await adapter.emit("最近 AI 行业有什么变化", chat_id="u1", user_id="u1")
        assert submitted == ["最近 AI 行业有什么变化"]

        async with db.session() as session:
            task = (await session.execute(select(Task))).scalar_one()
        assert await _wait(db, task.id, "succeeded") == "succeeded"
    finally:
        await queue.stop()
        await runner.aclose()

    try:
        assert len(adapter.sent) == 1
        chat_id, text, _ = adapter.sent[0]
        assert chat_id == "u1"
        assert "推理成本较上代下降约三成 [1]" in text
        assert "来源：" in text and url in text

        async with db.session() as session:
            delivery = (await session.execute(select(Delivery))).scalar_one()
        assert delivery.status == "sent"
        assert delivery.report_id is not None
        assert delivery.platform == "weixin"

        history = await sessions.load("weixin", "u1")
        assert [turn["role"] for turn in history] == ["user", "assistant"]
    finally:
        await db.dispose()


async def test_waiting_user_delivery_is_flushed_on_next_message(tmp_path: Path, local_server: str) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'gateway-flush.db'}")
    await db.init()
    try:
        router = Router(
            db,
            Authorizer({"weixin": {"u1"}}),
            DeliverySection(max_attempts=1, chunk_delay_seconds=0.0, fallback_platform=""),
        )
        adapter = FlakyAdapter()
        router.register(adapter)

        # 首次主动推送被平台拦下，进入 waiting_user
        assert await router.send(platform="weixin", chat_id="u1", text="今晚的日报", report_id=None) is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "waiting_user"

        # 用户发来消息后，router 先补投递再交给入站处理器
        seen: list[MessageEvent] = []

        async def inbound(event: MessageEvent) -> None:
            seen.append(event)

        router.set_inbound(inbound)
        await adapter.emit("在吗", chat_id="u1", user_id="u1")

        assert adapter.sent[:2] == [("u1", "今晚的日报", None), ("u1", "今晚的日报", None)]
        assert [event.text for event in seen] == ["在吗"]
    finally:
        await db.dispose()
