"""
模块: tests.unit.dispatcher.test_queue
职责: 校验任务队列的状态流转、重试、超时、取消与重启恢复
依赖: dispatcher.queue, core.db
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from newsbot.core.config import QueueSection
from newsbot.core.db import Database
from newsbot.core.errors import FatalError, RetriableError
from newsbot.core.models import Task
from newsbot.dispatcher.queue import TaskQueue


def _settings(**overrides: object) -> QueueSection:
    base = {"concurrency": 1, "task_timeout_seconds": 5, "max_attempts": 2, "retry_delay_seconds": 0.0}
    base.update(overrides)
    return QueueSection(**base)  # type: ignore[arg-type]


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await db.init()
    return db


async def _status(db: Database, task_id: int) -> tuple[str, int]:
    async with db.session() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task.status, task.attempts


async def _wait_status(db: Database, task_id: int, expected: str, timeout: float = 5.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    status = ""
    while asyncio.get_running_loop().time() < deadline:
        status, _ = await _status(db, task_id)
        if status == expected:
            return status
        await asyncio.sleep(0.02)
    return status


async def test_successful_task_marks_succeeded(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    seen: list[int] = []

    async def executor(session, task) -> str:
        seen.append(task.id)
        return "ok"

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert task.status == "pending"
        assert await _wait_status(db, task.id, "succeeded") == "succeeded"
        assert seen == [task.id]
    finally:
        await queue.stop()
        await db.dispose()


async def test_retriable_error_retries_then_fails(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    calls: list[int] = []

    async def executor(session, task) -> None:
        calls.append(task.id)
        raise RetriableError("上游抖动")

    queue = TaskQueue(db, _settings(max_attempts=2), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "failed") == "failed"
        status, attempts = await _status(db, task.id)
        assert status == "failed"
        assert attempts == 2
        assert len(calls) == 2
    finally:
        await queue.stop()
        await db.dispose()


async def test_retry_budget_from_config(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    calls: list[int] = []

    async def executor(session, task) -> None:
        calls.append(task.id)
        raise RetriableError("一直失败")

    queue = TaskQueue(db, _settings(max_attempts=3), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "failed") == "failed"
        assert len(calls) == 3
    finally:
        await queue.stop()
        await db.dispose()


async def test_fatal_error_fails_immediately(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    calls: list[int] = []

    async def executor(session, task) -> None:
        calls.append(task.id)
        raise FatalError("参数非法")

    queue = TaskQueue(db, _settings(max_attempts=5), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "failed") == "failed"
        assert len(calls) == 1
        async with db.session() as session:
            row = await session.get(Task, task.id)
            assert row is not None and row.error == "参数非法"
    finally:
        await queue.stop()
        await db.dispose()


async def test_unexpected_error_is_contained(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    async def executor(session, task) -> None:
        raise ValueError("意外错误")

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "failed") == "failed"
        async with db.session() as session:
            row = await session.get(Task, task.id)
            assert row is not None and "ValueError" in (row.error or "")
    finally:
        await queue.stop()
        await db.dispose()


async def test_timeout_marks_timeout(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    async def executor(session, task) -> None:
        await asyncio.sleep(5)

    queue = TaskQueue(db, _settings(task_timeout_seconds=1), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "timeout") == "timeout"
    finally:
        await queue.stop()
        await db.dispose()


async def test_timeout_error_type_marks_timeout(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    from newsbot.core.errors import TaskTimeoutError

    async def executor(session, task) -> None:
        raise TaskTimeoutError("太慢")

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "timeout") == "timeout"
    finally:
        await queue.stop()
        await db.dispose()


async def test_cancel_running_task(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    started = asyncio.Event()

    async def executor(session, task) -> None:
        started.set()
        await asyncio.sleep(10)

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        await asyncio.wait_for(started.wait(), timeout=3)
        assert await queue.cancel(task.id) is True
        status, _ = await _status(db, task.id)
        assert status == "cancelled"
    finally:
        await queue.stop()
        await db.dispose()


async def test_cancel_finished_or_missing_returns_false(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    async def executor(session, task) -> str:
        return "ok"

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        task = await queue.submit(kind="chat", query="q")
        assert await _wait_status(db, task.id, "succeeded") == "succeeded"
        assert await queue.cancel(task.id) is False
        assert await queue.cancel(9999) is False
    finally:
        await queue.stop()
        await db.dispose()


async def test_recover_requeues_unfinished(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    async with db.session() as session:
        session.add_all(
            [
                Task(kind="chat", query="残留任务", status="running"),
                Task(kind="chat", query="已完成", status="succeeded"),
            ]
        )
        await session.commit()

    seen: list[str] = []

    async def executor(session, task) -> str:
        seen.append(task.query)
        return "ok"

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    try:
        assert await _wait_status(db, 1, "succeeded") == "succeeded"
        assert seen == ["残留任务"]
    finally:
        await queue.stop()
        await db.dispose()


async def test_concurrency_limit(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    active = 0
    peak = 0

    async def executor(session, task) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.1)
        active -= 1
        return "ok"

    queue = TaskQueue(db, _settings(concurrency=2), executor)
    await queue.start()
    try:
        tasks = [await queue.submit(kind="chat", query=f"q{i}") for i in range(4)]
        for task in tasks:
            assert await _wait_status(db, task.id, "succeeded") == "succeeded"
        assert peak == 2
    finally:
        await queue.stop()
        await db.dispose()


async def test_stop_cancels_workers(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    async def executor(session, task) -> str:
        return "ok"

    queue = TaskQueue(db, _settings(), executor)
    await queue.start()
    await queue.stop()
    assert queue._workers == []
    async with db.session() as session:
        rows = (await session.execute(select(Task))).scalars().all()
    assert rows == []
    await db.dispose()
