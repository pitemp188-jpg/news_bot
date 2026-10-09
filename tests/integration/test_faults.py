"""
模块: tests.integration.test_faults
职责: 故障演练——LLM 瞬时故障与超时、适配器断线、浏览器崩溃、数据库锁，验证系统都能恢复
依赖: agent.runner, agent.tools.browser, core.db, dispatcher.queue, gateway.router
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.runner import Runner
from newsbot.agent.tools.browser import BrowserTool
from newsbot.core.config import AgentSection, DeliverySection, QueueSection
from newsbot.core.db import Database
from newsbot.core.errors import RetriableError
from newsbot.core.llm import LLMReply, ToolCall
from newsbot.core.models import Delivery, Task
from newsbot.dispatcher.queue import TaskQueue
from newsbot.gateway.auth import Authorizer
from newsbot.gateway.base import SendResult
from newsbot.gateway.router import Router

pytestmark = pytest.mark.integration

TERMINAL_STATES = {"succeeded", "failed", "timeout", "cancelled"}


@dataclass
class FlakyLLM:
    """前 failures 次调用抛 RetriableError，之后正常——模拟模型超时/限流。"""

    failures: int = 0
    content: str = "恢复后的答复"
    calls: int = 0

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        self.calls += 1
        if self.calls <= self.failures:
            raise RetriableError("模型瞬时故障: 请求超时")
        return reply(self.content)

    async def aclose(self) -> None:
        return None


async def _wait_status(db: Database, task_id: int, timeout: float = 10.0) -> str:
    """等到任务落到终态，返回最终状态。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = ""
    while loop.time() < deadline:
        async with db.session() as session:
            task = await session.get(Task, task_id)
            status = task.status if task else ""
        if status in TERMINAL_STATES:
            return status
        await asyncio.sleep(0.02)
    return status


def _queue(db: Database, llm: FlakyLLM, **overrides: Any) -> tuple[TaskQueue, Runner]:
    """真实 Runner + 真实队列，只把模型换成替身；返回两者便于断言。"""
    settings = QueueSection(concurrency=1, max_attempts=3, retry_delay_seconds=0.01, task_timeout_seconds=5.0)
    for key, value in overrides.items():
        setattr(settings, key, value)
    runner = Runner(llm, [FakeSearchTool(hits=[])], AgentSection())
    return TaskQueue(db, settings, _executor(runner)), runner


def _executor(runner: Runner):
    async def run_task(session: Any, task: Task) -> str | None:
        findings = await runner.run(task.query)
        return findings.answer

    return run_task


# ── 1. LLM 瞬时故障与超时 ──


async def test_transient_llm_failure_is_retried_then_succeeds(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-llm.db'}")
    await db.init()
    llm = FlakyLLM(failures=1)
    queue, _ = _queue(db, llm)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="今天有什么 AI 新闻")
        assert await _wait_status(db, task.id) == "succeeded"
        # 第一次失败后被队列重新执行，第二次成功
        assert llm.calls == 2
        row = await queue.find(task.id)
        assert row is not None and row.attempts == 2 and row.error is None
    finally:
        await queue.stop()
        await db.dispose()


async def test_exhausted_retries_fail_task_but_queue_survives(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-exhaust.db'}")
    await db.init()
    llm = FlakyLLM(failures=99)
    queue, _ = _queue(db, llm, max_attempts=2)
    await queue.start()
    try:
        dead = await queue.submit(kind="chat", query="注定失败的任务")
        assert await _wait_status(db, dead.id) == "failed"
        row = await queue.find(dead.id)
        assert row is not None and "重试 2 次仍失败" in (row.error or "")

        # 模型恢复后，队列仍能正常干活——失败没有污染 worker
        llm.failures = llm.calls
        alive = await queue.submit(kind="chat", query="恢复后的任务")
        assert await _wait_status(db, alive.id) == "succeeded"
        assert queue.depth == 0 and queue.running == 0
    finally:
        await queue.stop()
        await db.dispose()


async def test_hung_task_times_out_and_releases_slot(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-timeout.db'}")
    await db.init()
    settings = QueueSection(concurrency=1, max_attempts=1, retry_delay_seconds=0.01, task_timeout_seconds=1)

    async def hang(session: Any, task: Task) -> str | None:
        await asyncio.sleep(30)
        return "永远不会到这里"

    queue = TaskQueue(db, settings, hang)
    await queue.start()
    try:
        stuck = await queue.submit(kind="chat", query="卡住的任务")
        assert await _wait_status(db, stuck.id) == "timeout"
        row = await queue.find(stuck.id)
        assert row is not None and "1s" in (row.error or "")
        # 超时必须释放并发槽，否则后续任务会一直排队
        assert queue.running == 0

        queue.set_executor(_executor(Runner(FlakyLLM(), [], AgentSection())))
        following = await queue.submit(kind="chat", query="超时后的新任务")
        assert await _wait_status(db, following.id) == "succeeded"
    finally:
        await queue.stop()
        await db.dispose()


# ── 2. 适配器断线 ──


@dataclass
class BoomAdapter(FakeAdapter):
    """connect 直接抛异常——模拟网关拒绝连接。"""

    async def connect(self) -> bool:
        self.connect_calls += 1
        raise RuntimeError("网关拒绝连接")


@dataclass
class ExplodingAdapter(FakeAdapter):
    """send 抛异常——模拟连接在使用中被切断。"""

    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        self.sent.append((chat_id, text, reply_to))
        raise RuntimeError("连接已断开")


@dataclass
class RecordingSleeper:
    """把退避等待记录成列表，避免测试真的睡。"""

    waits: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


async def _router(db: Database, **settings: Any) -> Router:
    section = DeliverySection(chunk_delay_seconds=0.0, max_attempts=1, fallback_platform="qqbot")
    for key, value in settings.items():
        setattr(section, key, value)
    return Router(db, Authorizer({}), section, sleeper=RecordingSleeper())


async def test_adapter_connect_failure_keeps_other_platforms_online(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-connect.db'}")
    await db.init()
    router = await _router(db)
    broken = BoomAdapter(platform="weixin")
    healthy = FakeAdapter(platform="qqbot")
    router.register(broken)
    router.register(healthy)
    try:
        # 单个平台起不来不能拖垮整体
        online = await router.start()
        assert online == {"weixin": False, "qqbot": True}
    finally:
        await router.stop()
        await db.dispose()


async def test_disconnected_adapter_reports_failure_without_raising(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-offline.db'}")
    await db.init()
    router = await _router(db, fallback_platform="")
    adapter = FakeAdapter(platform="weixin", send_result=SendResult.failure("连接已断开", retryable=False))
    router.register(adapter)
    await adapter.connect()
    try:
        assert router.online() == {"weixin": True}
        await adapter.disconnect()
        assert router.online() == {"weixin": False}

        assert await router.send(platform="weixin", chat_id="u1", text="日报") is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "failed"
        assert row.error == "连接已断开"
    finally:
        await router.stop()
        await db.dispose()


async def test_send_exception_falls_back_to_secondary_platform(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fault-fallback.db'}")
    await db.init()
    router = await _router(db)
    primary = ExplodingAdapter(platform="weixin")
    backup = FakeAdapter(platform="qqbot")
    router.register(primary)
    router.register(backup)
    try:
        assert await router.send(platform="weixin", chat_id="u1", text="今晚的日报") is True
        # 主通道抛异常，备用通道真的收到了内容
        assert backup.sent == [("u1", "今晚的日报", None)]
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "sent"
    finally:
        await router.stop()
        await db.dispose()


# ── 3. 浏览器崩溃 ──


@dataclass
class CrashOnceRunner:
    """第一次调用崩溃，之后恢复——模拟浏览器进程被杀。"""

    crash_times: int = 1
    calls: int = 0
    closed: int = 0

    async def run(self, url: str, task: str) -> str:
        self.calls += 1
        if self.calls <= self.crash_times:
            raise RuntimeError("Browser closed unexpectedly")
        return "恢复后的页面正文"

    async def aclose(self) -> None:
        self.closed += 1


async def test_browser_crash_returns_failure_and_releases_slot() -> None:
    runner = CrashOnceRunner()
    tool = BrowserTool(AgentSection(browser_concurrency=1), runner=runner)

    crashed = await tool.run("https://example.com", task="提取正文")
    assert crashed.text.startswith("[工具执行失败]")
    assert "浏览器执行出错" in crashed.text
    assert "Browser closed unexpectedly" in crashed.text

    # 崩过之后信号量必须已释放，否则下一次调用会永久卡住
    recovered = await asyncio.wait_for(tool.run("https://example.com", task="提取正文"), timeout=2.0)
    assert recovered.text == "恢复后的页面正文"
    assert [source.url for source in recovered.sources] == ["https://example.com"]

    await tool.aclose()
    assert runner.closed == 1
    assert runner.calls == 2


async def test_agent_loop_survives_browser_crash() -> None:
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="browse", arguments={"url": "https://example.com"})]),
            reply("浏览器挂了，但根据已有信息给出结论 [1]"),
        ]
    )
    tool = BrowserTool(AgentSection(browser_concurrency=1), runner=CrashOnceRunner())
    runner = Runner(llm, [tool], AgentSection())

    findings = await runner.run("看看这个页面")
    # 工具失败不终止循环：失败信息回灌给模型，仍然产出最终答复
    assert not findings.budget_exhausted
    assert findings.answer == "浏览器挂了，但根据已有信息给出结论 [1]"
    assert "浏览器执行出错" in str(llm.calls[1][-1]["content"])


# ── 4. 数据库锁 ──


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    """另起一个连接并持有写锁，模拟并发写入。"""
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    return holder


async def test_write_waits_for_lock_instead_of_failing(tmp_path: Path) -> None:
    db_path = tmp_path / "fault-lock.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init()
    holder = _hold_write_lock(db_path)
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, holder.commit)  # 300ms 后让出锁

        # busy_timeout 会兜住这段等待；没有它这里会直接抛 database is locked
        async with db.session() as session:
            session.add(Task(kind="chat", query="锁竞争下的写入", status="pending"))
            await session.commit()

        async with db.session() as session:
            rows = (await session.execute(select(Task))).scalars().all()
        assert [row.query for row in rows] == ["锁竞争下的写入"]
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()
        await db.dispose()


async def test_reads_are_not_blocked_while_write_lock_is_held(tmp_path: Path) -> None:
    db_path = tmp_path / "fault-wal.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init()
    async with db.session() as session:
        session.add(Task(kind="chat", query="已有任务", status="pending"))
        await session.commit()

    holder = _hold_write_lock(db_path)
    try:
        # WAL 下读不阻塞；若退化成 journal 模式，这里会被写锁挡住
        async with db.session() as session:
            rows = (await session.execute(select(Task))).scalars().all()
        assert len(rows) == 1
    finally:
        holder.rollback()
        holder.close()
        await db.dispose()
