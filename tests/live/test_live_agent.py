"""
模块: tests.live.test_live_agent
职责: 真实 Agent 全链路——真实模型驱动规划循环，真实抓取公网页面，真实成稿与投递
依赖: agent.runner, agent.tools.*, result.*, dispatcher.*

搜索工具需要自建 SearXNG 或 Tavily 密钥；未配置时用固定命中列表替代搜索引擎，
其余环节（模型规划、抓取、去重、成稿、投递、落库）全部走真实实现。

运行：`uv run python -m pytest -m live tests/live`
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import pytest
from sqlalchemy import select

from newsbot.agent.runner import Runner
from newsbot.agent.tools.base import Source, ToolResult
from newsbot.agent.tools.fetch import FetchTool
from newsbot.core.config import AgentSection, QueueSection, Secrets, load_config
from newsbot.core.db import Database
from newsbot.core.llm import OpenAIClient
from newsbot.core.models import NewsItem, Report, Task
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.queue import TaskQueue
from newsbot.dispatcher.session import SessionStore
from newsbot.result.dedup import Deduper, Item, url_digest
from newsbot.result.report import ReportBuilder

pytestmark = pytest.mark.live

STABLE_URL = "https://example.com/"
STABLE_TITLE = "Example Domain"


class FixedSearch:
    """固定命中列表的搜索替身：把真实抓取交给 FetchTool，避免依赖外部搜索服务。"""

    name = "search"
    description = "返回预设的候选来源列表。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索关键词"}},
        "required": ["query"],
    }

    def __init__(self, hits: list[dict[str, str]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def run(self, query: str = "", **_: Any) -> ToolResult:
        self.queries.append(query)
        lines = [f"{index}. {hit['title']}\n   {hit['url']}" for index, hit in enumerate(self._hits, 1)]
        return ToolResult(
            text="\n".join(lines), sources=[Source(title=hit["title"], url=hit["url"]) for hit in self._hits]
        )

    async def aclose(self) -> None:
        return None


class RecordingSender:
    """记录投递内容的出站替身。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        self.sent.append((platform, chat_id, text))
        return True


def _secrets() -> Secrets:
    secrets = load_config().secrets
    if not secrets.llm_api_key:
        pytest.skip("未配置 LLM_API_KEY，跳过 live 测试")
    return secrets


def _make_deduper(db: Database) -> Callable[[], Awaitable[Deduper]]:
    async def factory() -> Deduper:
        deduper = Deduper()
        await deduper.load_recent(db, days=7)
        return deduper

    return factory


async def _wait(db: Database, task_id: int, expected: str, timeout: float) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = ""
    while loop.time() < deadline:
        async with db.session() as session:
            task = await session.get(Task, task_id)
            status = task.status if task else ""
        if status == expected:
            return status
        await asyncio.sleep(0.2)
    return status


async def test_live_agent_runs_full_loop() -> None:
    """真实模型 + 真实抓取：模型自主调用 fetch 抓 example.com，并据此给出带来源的答复。"""
    client = OpenAIClient(_secrets(), max_retries=2)
    search = FixedSearch([{"title": STABLE_TITLE, "url": STABLE_URL}])
    runner = Runner(
        client,
        [search, FetchTool(AgentSection(), allow_private=False)],
        AgentSection(max_steps=6, max_tokens=30_000),
    )
    try:
        findings = await runner.run(f"请抓取 {STABLE_URL} 并说明这个页面讲了什么")
    finally:
        await runner.aclose()
        await client.aclose()

    assert findings.answer.strip(), "模型没有产出结论"
    assert not findings.budget_exhausted
    assert 1 <= findings.steps <= 6
    assert findings.tokens > 0
    assert any(str(source.get("url", "")).startswith(STABLE_URL.rstrip("/")) for source in findings.sources), (
        findings.sources
    )
    assert "example" in findings.answer.lower() or "域名" in findings.answer


async def test_live_pipeline_produces_report_and_delivers(tmp_path) -> None:
    """真实流水线：队列 → 真实模型/抓取 → 成稿 → 投递 → 落库 report 与 news_item。"""
    client = OpenAIClient(_secrets(), max_retries=2)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    await db.init()
    sender = RecordingSender()
    runner = Runner(
        client,
        [FixedSearch([{"title": STABLE_TITLE, "url": STABLE_URL}]), FetchTool(AgentSection(), allow_private=False)],
        AgentSection(max_steps=6, max_tokens=30_000),
    )
    pipeline = Pipeline(
        db,
        runner=runner,
        reporter=ReportBuilder(client, max_chars=2000),
        sender=sender,
        sessions=SessionStore(db),
        deduper=_make_deduper(db),
    )
    queue = TaskQueue(db, QueueSection(concurrency=1, task_timeout_seconds=180), pipeline.execute)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="example.com 这个站点是做什么的", platform="fake", chat_id="c1")
        status = await _wait(db, task.id, "succeeded", timeout=180.0)
    finally:
        await queue.stop()
        await runner.aclose()
        await client.aclose()

    try:
        assert status == "succeeded"
        assert len(sender.sent) == 1
        _platform, _chat, text = sender.sent[0]
        assert "来源：" in text and "example.com" in text

        async with db.session() as session:
            report = (await session.execute(select(Report))).scalar_one()
            news = (await session.execute(select(NewsItem))).scalars().all()
            task_row = (await session.execute(select(Task))).scalar_one()
        assert report.task_id == task_row.id and report.sources
        assert [item.url for item in news] == [STABLE_URL]
    finally:
        await db.dispose()


async def test_live_dedup_blocks_repeated_source(tmp_path) -> None:
    """已入库来源在真实查库后会被判重，即使带上追踪参数。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-dedup.db'}")
    await db.init()
    try:
        async with db.session() as session:
            session.add(NewsItem(url=STABLE_URL, url_hash=url_digest(STABLE_URL), title=STABLE_TITLE))
            await session.commit()

        deduper = Deduper()
        assert await deduper.load_recent(db, days=7) == 1
        assert deduper.is_duplicate(Item(title="换个标题", url=f"{STABLE_URL}?utm_source=x"))
    finally:
        await db.dispose()
