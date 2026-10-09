"""
模块: tests.unit.agent.test_newsdb
职责: 校验资讯库查询的关键词过滤、天数过滤、来源返回与异常容错
依赖: agent.tools.newsdb, core.db
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from newsbot.agent.tools.newsdb import NewsDbTool
from newsbot.core.db import Database
from newsbot.core.models import NewsItem


async def _make_db(tmp_path: Path, items: list[NewsItem]) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'news.db'}")
    await db.init()
    async with db.session() as session:
        session.add_all(items)
        await session.commit()
    return db


def _now() -> datetime:
    return datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _item(title: str, url: str, days_ago: int = 0) -> NewsItem:
    return NewsItem(url=url, url_hash=url, title=title, fetched_at=_now() - timedelta(days=days_ago))


async def test_query_returns_matching_rows(tmp_path: Path) -> None:
    db = await _make_db(
        tmp_path,
        [
            _item("AI 行业周报", "https://a.example/1"),
            _item("体育赛事回顾", "https://a.example/2"),
        ],
    )
    try:
        tool = NewsDbTool(db, now=_now)
        result = await tool.run(keyword="AI")
        assert "AI 行业周报" in result.text
        assert "体育赛事回顾" not in result.text
        assert [source.url for source in result.sources] == ["https://a.example/1"]
    finally:
        await db.dispose()


async def test_old_items_are_excluded(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, [_item("很久以前", "https://a.example/old", days_ago=30)])
    try:
        tool = NewsDbTool(db, now=_now)
        assert (await tool.run(keyword="很久", days=7)).text == "资讯库中没有匹配的记录。"
        assert "很久以前" in (await tool.run(keyword="很久", days=40)).text
    finally:
        await db.dispose()


async def test_blank_keyword_matches_everything(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, [_item("一", "https://a.example/1"), _item("二", "https://a.example/2")])
    try:
        tool = NewsDbTool(db, now=_now)
        result = await tool.run()
        assert len(result.sources) == 2
    finally:
        await db.dispose()


async def test_no_rows_returns_plain_text(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, [])
    try:
        tool = NewsDbTool(db, now=_now)
        assert (await tool.run(keyword="无")).text == "资讯库中没有匹配的记录。"
    finally:
        await db.dispose()


async def test_database_error_is_contained() -> None:
    class BoomDb:
        @asynccontextmanager
        async def session(self) -> AsyncIterator[None]:
            raise RuntimeError("数据库挂了")
            yield  # pragma: no cover - 仅用于构成异步上下文管理器

    tool = NewsDbTool(BoomDb(), now=_now)  # type: ignore[arg-type]
    assert "资讯库查询失败" in (await tool.run(keyword="x")).text
