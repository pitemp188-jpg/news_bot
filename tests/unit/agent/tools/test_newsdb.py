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


# ── 新鲜度按"发布时间"而不是"抓取时间" ──
# 实测缺陷：过滤与排序用的是 `fetched_at`（我们抓到的时刻）。一篇几个月前的旧稿
# 今天被重新抓到时 fetched_at 就是今天，于是旧闻被算成新内容混进"最近一天"。


def _dated(title: str, url: str, *, published_days_ago: int, fetched_days_ago: int = 0) -> NewsItem:
    return NewsItem(
        url=url,
        url_hash=url,
        title=title,
        published_at=_now() - timedelta(days=published_days_ago),
        fetched_at=_now() - timedelta(days=fetched_days_ago),
    )


async def test_old_article_refetched_today_is_excluded(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, [_dated("半年前的 AI 旧闻", "https://a.example/old", published_days_ago=180)])
    try:
        tool = NewsDbTool(db, now=_now)
        assert (await tool.run(keyword="AI", days=7)).text == "资讯库中没有匹配的记录。"
        assert "半年前" in (await tool.run(keyword="AI", days=365)).text
    finally:
        await db.dispose()


async def test_missing_published_at_falls_back_to_fetched_at(tmp_path: Path) -> None:
    """历史数据的 published_at 是空的，此时按抓取时间判断，不能整条查不出来。"""
    db = await _make_db(tmp_path, [_item("没有发布时间的条目", "https://a.example/x")])
    try:
        tool = NewsDbTool(db, now=_now)
        assert "没有发布时间的条目" in (await tool.run(keyword="发布", days=7)).text
    finally:
        await db.dispose()


async def test_output_shows_date_and_origin(tmp_path: Path) -> None:
    item = _dated("AI 行业周报", "https://a.example/1", published_days_ago=1)
    item.source = "ifanr.com"
    db = await _make_db(tmp_path, [item])
    try:
        # 显式传时区：显示日期不能跟随宿主时区（CI 是 UTC、本机是 UTC+8）
        tool = NewsDbTool(db, now=_now, timezone="Asia/Shanghai")
        result = await tool.run(keyword="AI")
        assert "ifanr.com" in result.text, "来源名要显示出来"
        assert "10-08 20:00" in result.text, "UTC 12:00 应显示为北京 20:00"
        assert "日期未知" not in result.text
        assert result.sources[0].published.startswith("2026-10-08"), "发布日期要随来源传下去"
        assert result.sources[0].source == "ifanr.com"

        utc = NewsDbTool(db, now=_now, timezone="UTC")
        assert "10-08 12:00" in (await utc.run(keyword="AI")).text, "换时区结果跟着变"
    finally:
        await db.dispose()


async def test_unknown_publish_date_is_not_faked_with_crawl_time(tmp_path: Path) -> None:
    """发布时间未知时不能用抓取时间冒充。

    显示成抓取时间会把一篇半年前的旧稿说成是今天发的——"最新进展"就成了谎话。
    过滤允许退回抓取时间（历史数据不该整批丢），但展示必须如实。
    """
    db = await _make_db(tmp_path, [])
    try:
        item = _item("无日期条目", "https://a.example/nodate")
        item.published_at = None
        async with db.session() as session:
            session.add(item)
            await session.commit()
        tool = NewsDbTool(db, now=_now, timezone="Asia/Shanghai")
        result = await tool.run(keyword="无日期")
        assert "日期未知" in result.text
        assert "抓取于" in result.text, "抓取时间可以给，但必须标明它只是抓取时间"
        assert result.sources[0].published == "", "未知的发布时间不要编一个出来"
    finally:
        await db.dispose()
