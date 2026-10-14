"""
模块: tests.unit.dispatcher.test_ingest
职责: 校验来源条目入库——列表页过滤、网址去重、摘要与指纹落库
依赖: dispatcher.ingest
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import NewsItem
from newsbot.dispatcher.ingest import published_at, store_items


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}")
    await db.prepare()
    return db


async def _rows(db: Database) -> list[NewsItem]:
    async with db.session() as session:
        return list((await session.execute(select(NewsItem))).scalars())


async def test_store_items_skips_listing_pages(tmp_path: Path) -> None:
    """列表页 / 搜索页不入资讯库。

    Agent 为了找线索会去抓 GitHub Trending、财经频道首页、搜索结果页；这些页面一旦
    入库就变成"报道"，还得跟真文章一起去聚簇。实测 158 条里 11 条是这类页面，
    其中两条互相聚成了一个"事件"并占了热度榜首位。
    """
    db = await _make_db(tmp_path)
    try:
        written = await store_items(
            db,
            [
                {"title": "某公司发布芯片", "url": "https://example.com/2026/10/1.html", "source": "a"},
                {"title": "Trending  repositories on GitHub today · GitHub", "url": "https://github.com/trending"},
                {"title": "https://www.bing.com/news/search?q=x", "url": "https://www.bing.com/news/search?q=x"},
                {"title": "股票频道 - 东方财富网", "url": "https://stock.eastmoney.com/"},
            ],
        )
        assert written == 1
        assert [row.url for row in await _rows(db)] == ["https://example.com/2026/10/1.html"]
    finally:
        await db.dispose()


async def test_store_items_is_noop_when_all_filtered(tmp_path: Path) -> None:
    """全被过滤时不能留下空批次，也不能把网址当成内容塞进去。"""
    db = await _make_db(tmp_path)
    try:
        written = await store_items(
            db, [{"title": "AI News | TechCrunch", "url": "https://techcrunch.com/category/ai/"}]
        )
        assert written == 0
        assert await _rows(db) == []
    finally:
        await db.dispose()


async def test_store_items_dedupes_by_url(tmp_path: Path) -> None:
    """同一网址第二次入库要被跳过——采集每半小时跑一次，同一条会反复出现。"""
    db = await _make_db(tmp_path)
    try:
        item = {"title": "某公司发布芯片", "url": "https://example.com/a", "source": "a"}
        assert await store_items(db, [item]) == 1
        assert await store_items(db, [item]) == 0, "第二次不该再写一条"
        assert len(await _rows(db)) == 1
    finally:
        await db.dispose()


async def test_store_items_keeps_publish_date_and_summary(tmp_path: Path) -> None:
    """发布时间与摘要必须落库。

    发布时间是"这条新不新"的唯一依据（此前留空，模型把几天前的旧稿当最新进展）；
    摘要是聚簇判"是不是同一件事"的依据（只凭标题会漏掉中英文互译的同一件事）。
    """
    db = await _make_db(tmp_path)
    try:
        await store_items(
            db,
            [
                {
                    "title": "某公司发布芯片",
                    "url": "https://example.com/a",
                    "source": "a",
                    "snippet": "官方称性能提升三成",
                    "published": "2026-10-13T08:30:00Z",
                }
            ],
        )
        row = (await _rows(db))[0]
        assert row.summary == "官方称性能提升三成"
        assert row.published_at is not None and row.published_at.year == 2026
        assert row.content_hash and row.simhash, "指纹要落库，否则历史判重永远命中不了"
    finally:
        await db.dispose()


async def test_store_items_reports_zero_on_empty_input(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        assert await store_items(db, []) == 0
    finally:
        await db.dispose()


def test_published_at_tolerates_bad_input() -> None:
    """解析不了就返回 None，不能让整批入库失败（有的源不给日期或格式不规范）。"""
    assert published_at(None) is None
    assert published_at("") is None
    assert published_at("不是日期") is None
    parsed: Any = published_at("2026-10-13T08:30:00Z")
    assert parsed is not None and parsed.tzinfo is not None, "必须带时区，否则热度窗口会算错"
    naive = published_at("2026-10-13T08:30:00")
    assert naive is not None and naive.tzinfo is not None, "无时区的按 UTC 解释"
