"""
模块: tests.unit.core.test_db
职责: 校验建表、WAL 开启、会话读写与模型默认值
依赖: core.db, core.models
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text

from newsbot.core.db import Database
from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task


async def test_init_and_crud(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.init()
    try:
        async with db.session() as session:
            task = Task(kind="chat", query="今天 AI 新闻", status="pending")
            session.add(task)
            await session.commit()

            stored = (await session.execute(select(Task))).scalar_one()
        assert stored.id == 1
        assert stored.attempts == 0
        assert stored.created_at is not None
        assert stored.started_at is None
    finally:
        await db.dispose()


async def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}")
    await db.init()
    try:
        async with db.session() as session:
            mode = str((await session.execute(text("PRAGMA journal_mode"))).scalar_one())
        assert mode.lower() == "wal"
    finally:
        await db.dispose()
    assert (tmp_path / "wal.db").exists()


async def test_new_models_defaults(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'models.db'}")
    await db.init()
    try:
        async with db.session() as session:
            session.add_all(
                [
                    Schedule(cron="0 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1"),
                    NewsItem(url="https://example.com/a", url_hash="h1", title="标题"),
                    Report(content="日报", sources=[{"title": "x", "url": "https://example.com/a"}]),
                    Delivery(platform="qqbot", chat_id="c1", status="pending"),
                ]
            )
            await session.commit()
            schedule = (await session.execute(select(Schedule))).scalar_one()
            item = (await session.execute(select(NewsItem))).scalar_one()
        assert schedule.enabled is True
        assert schedule.topics == ["AI"]
        assert item.content_hash == ""
    finally:
        await db.dispose()
