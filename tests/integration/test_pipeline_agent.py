"""
模块: tests.integration.test_pipeline_agent
职责: 集成校验——真实队列 + 真实 Agent 抓取本地站点 + 真实去重成稿 + 替身投递
依赖: dispatcher.queue, dispatcher.pipeline, agent.runner, result.report
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.runner import Runner
from newsbot.agent.tools.fetch import FetchTool
from newsbot.core.config import AgentSection, QueueSection
from newsbot.core.db import Database
from newsbot.core.llm import ToolCall
from newsbot.core.models import Report, Task
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.queue import TaskQueue
from newsbot.dispatcher.session import SessionStore
from newsbot.result.dedup import Deduper, Item
from newsbot.result.report import ReportBuilder

pytestmark = pytest.mark.integration


class RecordingSender:
    """记录投递内容，模拟网关出站。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str, str]] = []
        self.report_ids: list[int | None] = []

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        self.sent.append((platform, chat_id, text))
        self.report_ids.append(report_id)
        return self.ok


async def _wait(db: Database, task_id: int, expected: str, timeout: float = 10.0) -> str:
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


async def test_full_chain_from_queue_to_delivery(tmp_path: Path, local_server: str) -> None:
    url = f"{local_server}/article.html"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'chain.db'}")
    await db.init()

    search = FakeSearchTool(hits=[{"title": "示例资讯：AI 行业周报", "url": url, "snippet": "摘要"}])
    runner = Runner(
        FakeLLM(
            replies=[
                reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI"})]),
                reply("", calls=[ToolCall(id="c2", name="fetch", arguments={"url": url})]),
                reply("推理成本较上代下降约三成 [1]"),
            ]
        ),
        [search, FetchTool(AgentSection(), allow_private=True)],
        AgentSection(max_steps=6, max_tokens=10_000),
    )
    sender = RecordingSender()
    deduper = Deduper()
    pipeline = Pipeline(
        db,
        runner=runner,
        reporter=ReportBuilder(),
        sender=sender,
        sessions=SessionStore(db),
    )
    queue = TaskQueue(db, QueueSection(concurrency=1, task_timeout_seconds=20), pipeline.execute)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="最近 AI 行业有什么变化", platform="qqbot", chat_id="c1")
        assert await _wait(db, task.id, "succeeded") == "succeeded"
    finally:
        await queue.stop()
        await runner.aclose()

    try:
        # 投递内容包含结论、来源编号与来源明细
        assert len(sender.sent) == 1
        platform, chat_id, text = sender.sent[0]
        assert (platform, chat_id) == ("qqbot", "c1")
        assert "推理成本较上代下降约三成 [1]" in text
        assert "来源：" in text
        assert url in text

        # 报告与会话均已落库，可直接支持追问
        async with db.session() as session:
            report = (await session.execute(select(Report))).scalar_one()
            task_row = (await session.execute(select(Task))).scalar_one()
        assert report.task_id == task_row.id
        assert report.sources == [{"title": "示例资讯：AI 行业周报", "url": url}]
        history = await SessionStore(db).load("qqbot", "c1")
        assert [turn["role"] for turn in history] == ["user", "assistant"]
        assert history[0]["content"] == "最近 AI 行业有什么变化"

        # 同一条资讯再次收集时被去重拦下，不会重复推送
        assert await deduper.load_recent(db, days=7) == 0  # 资讯库由采集环节另行落库
        deduper.remember(Item(title="示例资讯：AI 行业周报", url=url, text="推理成本较上代下降约三成"))
        assert deduper.is_duplicate(Item(title="换个标题", url=f"{url}?utm_source=x", text="完全不同"))
    finally:
        await db.dispose()


async def test_delivery_failure_keeps_task_failed(tmp_path: Path, local_server: str) -> None:
    url = f"{local_server}/article.html"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'chain-fail.db'}")
    await db.init()
    runner = Runner(
        FakeLLM(replies=[reply("直接给结论 [1]")]),
        [FakeSearchTool(hits=[{"title": "示例", "url": url, "snippet": "s"}])],
        AgentSection(),
    )
    pipeline = Pipeline(
        db,
        runner=runner,
        reporter=ReportBuilder(),
        sender=RecordingSender(ok=False),
        sessions=SessionStore(db),
    )
    queue = TaskQueue(db, QueueSection(concurrency=1, max_attempts=1), pipeline.execute)
    await queue.start()
    try:
        task = await queue.submit(kind="scheduled", query="AI 日报", platform="weixin", chat_id="u1")
        assert await _wait(db, task.id, "failed") == "failed"
        async with db.session() as session:
            row = await session.get(Task, task.id)
            assert row is not None and "投递失败" in (row.error or "")
    finally:
        await queue.stop()
        await runner.aclose()
        await db.dispose()
