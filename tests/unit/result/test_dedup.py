"""
模块: tests.unit.result.test_dedup
职责: 校验 URL 归一化、内容指纹、simhash 近似去重与历史指纹载入
依赖: result.dedup
"""

from __future__ import annotations

from pathlib import Path

from newsbot.core.db import Database
from newsbot.core.models import NewsItem
from newsbot.result.dedup import (
    Deduper,
    Item,
    content_digest,
    hamming,
    normalize_url,
    simhash64,
    simhash_hex,
    url_digest,
)


def test_normalize_drops_tracking_and_fragment() -> None:
    normalized = normalize_url("HTTPS://News.Example.com/a/?utm_source=x&id=2&from=qq#top")
    assert normalized == "https://news.example.com/a?id=2"


def test_normalize_keeps_meaningful_query() -> None:
    assert normalize_url("https://a.example/p?page=2") == "https://a.example/p?page=2"


def test_normalize_root_path() -> None:
    assert normalize_url("https://a.example") == "https://a.example/"


def test_same_page_different_tracking_same_digest() -> None:
    left = url_digest("https://a.example/x?utm_medium=wechat")
    right = url_digest("https://A.example/x")
    assert left == right


def test_content_digest_ignores_whitespace_and_case() -> None:
    assert content_digest("Hello   World") == content_digest("hello world")


def test_simhash_is_stable_and_similar_for_near_text() -> None:
    base = simhash64("人工智能模型推理成本下降三成")
    assert base == simhash64("人工智能模型推理成本下降三成")
    near = simhash64("人工智能模型推理成本大幅下降三成")
    far = simhash64("今天天气不错适合出门跑步")
    assert hamming(base, near) < hamming(base, far)


def test_simhash_of_empty_text_is_zero() -> None:
    assert simhash64("") == 0
    assert simhash_hex(0) == "0" * 16


def test_duplicate_by_url_and_by_content() -> None:
    deduper = Deduper()
    deduper.remember(Item(title="标题", url="https://a.example/1", text="正文内容"))
    assert deduper.is_duplicate(Item(title="另一个标题", url="https://a.example/1?utm_source=x", text="完全不同"))
    assert deduper.is_duplicate(Item(title="标题2", url="https://a.example/2", text="  正文内容  "))
    assert not deduper.is_duplicate(Item(title="新标题", url="https://a.example/3", text="全新正文"))


def test_near_duplicate_is_detected_by_simhash() -> None:
    deduper = Deduper()
    deduper.remember(Item(title="t", url="https://a.example/1", text="人工智能模型推理成本下降三成"))
    near = Item(title="t2", url="https://a.example/2", text="人工智能模型推理成本大幅下降三成")
    assert deduper.is_duplicate(near) is True


def test_short_texts_are_not_treated_as_near_duplicates() -> None:
    deduper = Deduper()
    deduper.remember(Item(title="AI", url="https://a.example/1", text="AI"))
    assert deduper.is_duplicate(Item(title="AI 行业周报", url="https://a.example/2", text="AI 行业周报")) is False


def test_filter_keeps_first_and_drops_repeats() -> None:
    deduper = Deduper()
    items = [
        Item(title="一", url="https://a.example/1", text="内容一"),
        Item(title="一", url="https://a.example/1", text="内容一"),
        Item(title="二", url="https://a.example/2", text="内容二"),
    ]
    kept = deduper.filter(items)
    assert [item.title for item in kept] == ["一", "二"]


async def test_load_recent_populates_fingerprints(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'dedup.db'}")
    await db.init()
    try:
        async with db.session() as session:
            session.add(
                NewsItem(
                    url="https://a.example/old",
                    url_hash=url_digest("https://a.example/old"),
                    title="旧闻",
                    content_hash=content_digest("旧闻正文"),
                    simhash=simhash_hex(simhash64("旧闻正文")),
                )
            )
            await session.commit()
        deduper = Deduper()
        assert await deduper.load_recent(db, days=7) == 1
        assert deduper.is_duplicate(Item(title="旧闻", url="https://a.example/old", text="任意"))
        assert deduper.is_duplicate(Item(title="旧闻", url="https://a.example/other", text="旧闻正文"))
    finally:
        await db.dispose()


async def test_load_recent_skips_old_rows(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'dedup-old.db'}")
    await db.init()
    try:
        async with db.session() as session:
            session.add(
                NewsItem(
                    url="https://a.example/ancient",
                    url_hash="h",
                    title="远古",
                    fetched_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
            await session.commit()
        assert await Deduper().load_recent(db, days=7) == 0
    finally:
        await db.dispose()
