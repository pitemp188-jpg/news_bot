"""
模块: tests.unit.result.test_board
职责: 校验事件层的聊天呈现——热度榜/日报渲染成可直接推送的文本
依赖: result.board
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import NewsItem, ReportItem, Story, StoryScore
from newsbot.result import board

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


async def _make_db(tmp_path: Any) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'board.db'}")
    await db.prepare()
    return db


async def _seed(
    db: Database,
    *,
    title: str,
    url: str,
    source: str,
    hours: float = 1,
    digest: str | None = None,
    selected: bool = True,
) -> int:
    """建一间事件 + 一条已归组且已打分的报道，返回 story_id。

    `hours` 控制报道时间，用来构造热度差异（半衰期 24 小时）。
    """
    moment = NOW - timedelta(hours=hours)
    async with db.session() as session:
        item = NewsItem(
            url=url,
            url_hash=url,
            title=title,
            source=source,
            summary="官方称性能提升三成",
            published_at=moment,
            fetched_at=moment,
        )
        session.add(item)
        await session.flush()
        story = Story(title=title, first_seen_at=moment, last_seen_at=moment, digest=digest)
        session.add(story)
        await session.flush()
        session.add(
            ReportItem(
                story_id=story.id,
                news_item_id=item.id,
                participant=source,
                tier="T2",
                role="primary",
                editorial=True,
                observed_at=moment,
            )
        )
        session.add(StoryScore(news_item_id=item.id, score1=9, score2=9, selected=selected))
        await session.commit()
        return story.id


async def _join(db: Database, story_id: int, *, title: str, url: str, source: str, hours: float = 1) -> None:
    """给已有事件再加一家来源，让它够进榜门槛（参与者 ≥2）。"""
    moment = NOW - timedelta(hours=hours)
    async with db.session() as session:
        item = NewsItem(
            url=url,
            url_hash=url,
            title=title,
            source=source,
            summary="另一家的说法",
            published_at=moment,
            fetched_at=moment,
        )
        session.add(item)
        await session.flush()
        session.add(
            ReportItem(
                story_id=story_id,
                news_item_id=item.id,
                participant=source,
                tier="T2",
                role="duplicate",
                editorial=True,
                observed_at=moment,
            )
        )
        session.add(StoryScore(news_item_id=item.id, score1=8, score2=8, selected=True))
        await session.commit()


# ── 热度榜文本 ──


async def test_hot_text_lists_entries_with_metrics(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(db, title="两家都报的这件事", url="https://a.example/1", source="a.com", hours=2)
        await _join(db, story_id, title="另一家的说法", url="https://b.example/1", source="b.com")

        text = await board.hot_text(db, now=NOW)
        assert "热度榜" in text
        assert "两家都报的这件事" in text
        assert "2 家报道" in text
        assert "按独立来源计数" in text, "规则要摊开写，读者才知道这个榜单是什么口径"
        assert "https://b.example/1" in text, "链接应取最新那条报道"
        assert "https://a.example/1" not in text
    finally:
        await db.dispose()


async def test_hot_text_is_empty_with_a_reason(tmp_path: Any) -> None:
    """空榜要给原因，不能只说"空"。进榜要求参与者 ≥2——独家报道不是热点。"""
    db = await _make_db(tmp_path)
    try:
        await _seed(db, title="只有一家报的事", url="https://a.example/1", source="a.com")
        text = await board.hot_text(db, now=NOW)
        assert "为空" in text
        assert "两家独立来源" in text, "要说明为什么空"
        assert "只有一家报的事" not in text
    finally:
        await db.dispose()


async def test_hot_text_respects_limit(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        for index in range(5):
            story_id = await _seed(
                db, title=f"事件{index}", url=f"https://a{index}.example/1", source=f"a{index}.com", hours=index + 1
            )
            await _join(
                db, story_id, title=f"事件{index}另一家", url=f"https://b{index}.example/1", source=f"b{index}.com"
            )
        text = await board.hot_text(db, limit=2, now=NOW)
        assert text.count("\n1. ") == 1 and text.count("\n2. ") == 1
        assert "\n3. " not in text
    finally:
        await db.dispose()


async def test_hot_text_truncates_long_titles(tmp_path: Any) -> None:
    """长英文标题必须在聊天里截断：折行错乱比信息略少更糟。"""
    db = await _make_db(tmp_path)
    try:
        long_title = "Anthropic can't reliably control its AI agents and it is cutting off internal evals"
        story_id = await _seed(db, title=long_title, url="https://a.example/1", source="a.com")
        await _join(db, story_id, title="另一家", url="https://b.example/1", source="b.com")
        text = await board.hot_text(db, now=NOW)
        assert "…" in text
        assert long_title not in text
    finally:
        await db.dispose()


async def test_hot_text_omits_flat_trend(tmp_path: Any) -> None:
    """趋势为持平时不写——每条都写"持平"等于没写。"""
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(db, title="刚出现的事", url="https://a.example/1", source="a.com")
        await _join(db, story_id, title="另一家", url="https://b.example/1", source="b.com")
        text = await board.hot_text(db, now=NOW)
        assert "持平" not in text
    finally:
        await db.dispose()


# ── 日报文本 ──


async def test_digest_text_uses_story_summary(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(
            db,
            title="某公司发布芯片",
            url="https://a.example/1",
            source="a.com",
            digest="某公司发布新一代推理芯片，性能提升三成。",
        )
        await _join(db, story_id, title="另一家也报了", url="https://b.example/1", source="b.com")

        text = await board.digest_text(db, now=NOW)
        assert "日报" in text
        assert "某公司发布芯片" in text
        assert "某公司发布新一代推理芯片，性能提升三成。" in text
    finally:
        await db.dispose()


async def test_digest_text_excludes_unselected(tmp_path: Any) -> None:
    """日报只收通过精选的事件——不读入选判定会让精选对日报毫无影响。"""
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(db, title="没入选的事", url="https://a.example/1", source="a.com", selected=False)
        await _join(db, story_id, title="另一家也报了", url="https://b.example/1", source="b.com")
        async with db.session() as session:
            for row in (await session.scalars(select(StoryScore))).all():
                row.selected = False
            await session.commit()
        text = await board.digest_text(db, now=NOW)
        assert "没入选的事" not in text
        assert "没有达到入选标准" in text
    finally:
        await db.dispose()


# ── 分发 ──


async def test_render_dispatches_by_kind(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(db, title="某公司发布芯片", url="https://a.example/1", source="a.com")
        await _join(db, story_id, title="另一家", url="https://b.example/1", source="b.com")
        assert "热度榜" in await board.render(db, "hot", now=NOW)
        assert "日报" in await board.render(db, "digest", now=NOW)
    finally:
        await db.dispose()


async def test_render_rejects_unknown_kind(tmp_path: Any) -> None:
    """不认识的类型要抛错，不能静默返回空串——那会让投递一条空消息。"""
    db = await _make_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            await board.render(db, "搜索", now=NOW)
    finally:
        await db.dispose()


def test_board_topics_only_map_to_known_kinds() -> None:
    """订阅关键词的取值必须都是渲染认得出来的类型。"""
    assert set(board.BOARD_TOPICS.values()) <= board.BOARD_KINDS
    assert board.BOARD_TOPICS["热榜"] == "hot"
    assert board.BOARD_TOPICS["日报"] == "digest"


# ── 展示时区 ──


async def test_hot_text_renders_the_header_in_the_display_timezone(tmp_path: Any) -> None:
    """表头时刻按**展示时区**渲染。

    回归用例：`moment` 默认取 UTC 当下，若直接格式化，UTC+8 的用户会看到早 8 小时的
    时间，跨日时连日期都对不上（同类缺陷见 T11.10 的 `astimezone()`）。断言用
    「相差 8 小时的同一时刻在 UTC 与 Asia/Shanghai 下渲染出不同文本」来表达，
    不写死具体字符串，免得换个固定时刻就要改用例。
    """
    db = await _make_db(tmp_path)
    try:
        story_id = await _seed(db, title="两家都报的这件事", url="https://a.example/1", source="a.com")
        await _join(db, story_id, title="另一家也报了", url="https://b.example/1", source="b.com")
        utc_text = await board.hot_text(db, now=NOW, timezone="UTC")
        local_text = await board.hot_text(db, now=NOW, timezone="Asia/Shanghai")
        assert "热度榜 · 10-13 12:00" in utc_text
        assert "热度榜 · 10-13 20:00" in local_text
        assert utc_text != local_text
    finally:
        await db.dispose()


async def test_digest_text_renders_the_date_in_the_display_timezone(tmp_path: Any) -> None:
    """日报标题上的日期同样按展示时区——UTC 的 23:30 在上海已经是第二天。"""
    db = await _make_db(tmp_path)
    try:
        await _seed(db, title="某公司发布芯片", url="https://a.example/1", source="a.com")
        late = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
        assert "2026-10-13 日报" in await board.digest_text(db, now=late, timezone="UTC")
        assert "2026-10-14 日报" in await board.digest_text(db, now=late, timezone="Asia/Shanghai")
    finally:
        await db.dispose()
