"""
模块: tests.unit.core.test_db
职责: 校验建表、WAL 开启、会话读写与模型默认值
依赖: core.db, core.models
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from newsbot.core.db import Database
from newsbot.core.models import (
    Delivery,
    HotSnapshot,
    NewsItem,
    Report,
    ReportItem,
    Schedule,
    Story,
    StoryScore,
    Task,
)


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
            busy = int((await session.execute(text("PRAGMA busy_timeout"))).scalar_one())
        assert mode.lower() == "wal"
        assert busy >= 1000
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


# ── 结构升级（prepare）──
# 实测缺陷：`create_all` 只建缺失的**表**，对已存在的表一个列都不动。所以给模型加字段后，
# 老库（本地那个跑了两天的库、服务器上那个）拿不到新列，服务一启动就在查询时报
# `no such column`。本地库正是这个状态：表是 create_all 建的、alembic 记账为空。


async def _legacy_db(target: Path) -> None:
    """造一个"老库"：缺列的 news_item 与 task，带一条已有数据。"""
    import sqlite3

    raw = sqlite3.connect(target)
    raw.execute("CREATE TABLE news_item (id INTEGER PRIMARY KEY, url TEXT, url_hash TEXT, title TEXT)")
    raw.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, kind TEXT)")
    raw.execute("INSERT INTO news_item (url, url_hash, title) VALUES ('https://a/1', 'h1', '旧数据')")
    raw.commit()
    raw.close()


async def test_prepare_adds_missing_columns_to_existing_table(tmp_path: Path) -> None:
    target = tmp_path / "legacy.db"
    await _legacy_db(target)
    db = Database(f"sqlite+aiosqlite:///{target}")
    try:
        await db.prepare()
        async with db.session() as session:
            columns = {row[1] for row in (await session.execute(text("PRAGMA table_info(news_item)"))).fetchall()}
            titles = (await session.execute(text("select title from news_item"))).fetchall()
        # ORM 里 news_item 有的字段，老库都得补上
        assert {"published_at", "source", "content_hash", "simhash", "fetched_at"} <= columns
        assert [row[0] for row in titles] == ["旧数据"], "补列不能动已有数据"
    finally:
        await db.dispose()


async def test_prepare_creates_new_tables(tmp_path: Path) -> None:
    target = tmp_path / "legacy2.db"
    await _legacy_db(target)
    db = Database(f"sqlite+aiosqlite:///{target}")
    try:
        await db.prepare()
        async with db.session() as session:
            tables = {
                row[0]
                for row in (await session.execute(text("select name from sqlite_master where type='table'"))).fetchall()
            }
        assert {"story", "report_item", "story_score", "hot_snapshot"} <= tables
    finally:
        await db.dispose()


async def test_prepare_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "twice.db"
    await _legacy_db(target)
    db = Database(f"sqlite+aiosqlite:///{target}")
    try:
        await db.prepare()
        first = await db._add_missing_columns()
        assert first == [], f"第二次不应再补任何列，实际补了 {first}"
        assert await db._add_missing_columns() == []
    finally:
        await db.dispose()


async def test_prepare_on_fresh_db(tmp_path: Path) -> None:
    """全新库：建出全部表，不报错。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    try:
        await db.prepare()
        async with db.session() as session:
            count = (await session.execute(text("select count(*) from news_item"))).scalar_one()
        assert count == 0
    finally:
        await db.dispose()


# ── 事件层模型 ──


async def test_event_layer_defaults(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await db.prepare()
    try:
        async with db.session() as session:
            item = NewsItem(url="https://a/1", url_hash="h1", title="标题")
            session.add(item)
            await session.flush()
            story = Story(title="某公司发布新模型")
            session.add(story)
            await session.flush()
            session.add(ReportItem(story_id=story.id, news_item_id=item.id, participant="qbitai", tier="T2"))
            session.add(StoryScore(news_item_id=item.id, score1=72, score2=68))
            session.add(HotSnapshot(story_id=story.id, hour=datetime(2026, 10, 12, 3, tzinfo=UTC), heat=1.5))
            await session.commit()

            stored = (await session.execute(select(ReportItem))).scalar_one()
            score = (await session.execute(select(StoryScore))).scalar_one()
        assert stored.role == "duplicate"  # 默认按"重复报道"算，只有明确是首发/进展才升级
        assert stored.editorial is True
        assert stored.observed_at is not None
        assert score.selected is False
        assert (score.score1 + score.score2) / 2 == 70
    finally:
        await db.dispose()


async def test_report_item_is_unique_per_story(tmp_path: Path) -> None:
    """同一篇文章不能挂到同一事件两次：重复归组必须幂等，否则热度会重复计算。"""
    from sqlalchemy.exc import IntegrityError

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'uniq.db'}")
    await db.prepare()
    try:
        async with db.session() as session:
            item = NewsItem(url="https://a/1", url_hash="h1")
            session.add(item)
            await session.flush()
            story = Story(title="t")
            session.add(story)
            await session.flush()
            session.add(ReportItem(story_id=story.id, news_item_id=item.id))
            await session.commit()
            session.add(ReportItem(story_id=story.id, news_item_id=item.id))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await db.dispose()
