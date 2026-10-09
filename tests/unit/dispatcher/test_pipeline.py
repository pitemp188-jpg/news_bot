"""
模块: tests.unit.dispatcher.test_pipeline
职责: 校验流水线顺序、报告落库、会话追加与投递失败重试分类
依赖: dispatcher.pipeline, core.db
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.errors import RetriableError
from newsbot.core.models import Report, Task
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.session import SessionStore


@dataclass
class Built:
    content: str = "报告正文"
    sources: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = [{"title": "标题", "url": "https://example.com/a"}]


class FakeRunner:
    def __init__(self, findings: Any = "findings") -> None:
        self.findings = findings
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def run(self, query: str, history: list[dict[str, Any]]) -> Any:
        self.calls.append((query, history))
        return self.findings


class FakeReporter:
    def __init__(self, built: Built | None = None) -> None:
        self.built = built or Built()
        self.seen: list[Any] = []

    async def build(self, task: Task, findings: Any) -> Built:
        self.seen.append(findings)
        return self.built


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str, str]] = []
        self.report_ids: list[int | None] = []

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        self.sent.append((platform, chat_id, text))
        self.report_ids.append(report_id)
        return self.ok


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    await db.init()
    return db


async def _make_task(db: Database, **overrides: Any) -> int:
    payload = {"kind": "chat", "query": "今天 AI 新闻", "status": "running", "platform": "qqbot", "chat_id": "c1"}
    payload.update(overrides)
    async with db.session() as session:
        task = Task(**payload)  # type: ignore[arg-type]
        session.add(task)
        await session.commit()
        return task.id


async def test_full_chain_persists_and_delivers(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        runner, reporter, sender = FakeRunner(), FakeReporter(), FakeSender()
        sessions = SessionStore(db)
        pipeline = Pipeline(db, runner=runner, reporter=reporter, sender=sender, sessions=sessions)
        task_id = await _make_task(db)

        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            content = await pipeline.execute(session, task)

        assert content == "报告正文"
        assert sender.sent == [("qqbot", "c1", "报告正文")]
        assert reporter.seen == ["findings"]
        async with db.session() as session:
            report = (await session.execute(select(Report))).scalar_one()
        assert report.task_id == task_id
        assert report.sources == [{"title": "标题", "url": "https://example.com/a"}]
        assert await sessions.load("qqbot", "c1") == [
            {"role": "user", "content": "今天 AI 新闻"},
            {"role": "assistant", "content": "报告正文"},
        ]
    finally:
        await db.dispose()


async def test_model_usage_is_persisted(tmp_path: Path) -> None:
    """每次模型调用的用量都要落库，否则后台的成本统计永远是 0（回归用例）。"""
    from newsbot.agent.runner import Findings
    from newsbot.core.llm import Usage
    from newsbot.core.models import LlmUsage

    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论",
            usages=[
                Usage(prompt_tokens=120, completion_tokens=30, model="deepseek-v4-flash"),
                Usage(prompt_tokens=200, completion_tokens=80, model="deepseek-v4-flash"),
            ],
        )
        pipeline = Pipeline(
            db, runner=FakeRunner(findings), reporter=FakeReporter(), sender=FakeSender(), sessions=SessionStore(db)
        )
        task_id = await _make_task(db)
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            await pipeline.execute(session, task)

        async with db.session() as session:
            rows = (await session.execute(select(LlmUsage).order_by(LlmUsage.id))).scalars().all()
        assert [(row.prompt_tokens, row.completion_tokens) for row in rows] == [(120, 30), (200, 80)]
        assert {row.model for row in rows} == {"deepseek-v4-flash"}
        assert {row.task_id for row in rows} == {task_id}
    finally:
        await db.dispose()


async def test_runner_without_usage_writes_nothing(tmp_path: Path) -> None:
    """没有用量信息时不要写空记录。"""
    from newsbot.core.models import LlmUsage

    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(
            db, runner=FakeRunner("findings"), reporter=FakeReporter(), sender=FakeSender(), sessions=SessionStore(db)
        )
        task_id = await _make_task(db)
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            await pipeline.execute(session, task)
        async with db.session() as session:
            assert list((await session.execute(select(LlmUsage))).scalars()) == []
    finally:
        await db.dispose()


async def test_history_is_passed_to_runner(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        runner = FakeRunner()
        sessions = SessionStore(db)
        await sessions.append("qqbot", "c1", user_text="上一问", reply_text="上一答")
        pipeline = Pipeline(db, runner=runner, reporter=FakeReporter(), sender=FakeSender(), sessions=sessions)

        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            await pipeline.execute(session, task)

        assert runner.calls[0][1][0]["content"] == "上一问"
    finally:
        await db.dispose()


async def test_delivery_failure_is_retriable(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(
            db,
            runner=FakeRunner(),
            reporter=FakeReporter(),
            sender=FakeSender(ok=False),
            sessions=SessionStore(db),
        )
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            with pytest.raises(RetriableError):
                await pipeline.execute(session, task)
        # 报告在投递前已提交（避免网络 IO 期间持有写事务），但会话历史不追加
        async with db.session() as session:
            assert len((await session.execute(select(Report))).scalars().all()) == 1
        assert await SessionStore(db).load("qqbot", "c1") == []
    finally:
        await db.dispose()


async def test_missing_sender_is_retriable(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(db, runner=FakeRunner(), reporter=FakeReporter(), sender=None, sessions=SessionStore(db))
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            with pytest.raises(RetriableError):
                await pipeline.execute(session, task)
    finally:
        await db.dispose()


async def test_task_without_target_only_reports(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        sender = FakeSender()
        pipeline = Pipeline(db, runner=FakeRunner(), reporter=FakeReporter(), sender=sender, sessions=SessionStore(db))
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db, platform=None, chat_id=None))
            assert task is not None
            await pipeline.execute(session, task)
        assert sender.sent == []
        async with db.session() as session:
            assert len((await session.execute(select(Report))).scalars().all()) == 1
    finally:
        await db.dispose()
