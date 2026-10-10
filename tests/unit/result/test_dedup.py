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
    _PARAPHRASE_SIMILARITY,
    Deduper,
    Item,
    _tokens,
    content_digest,
    hamming,
    identifiers,
    jaccard,
    normalize_url,
    same_story,
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


# ── 内容去重：同一件事的多家报道合并，来源不同不算重复 ──


def test_identifiers_keep_version_strings_and_drop_years() -> None:
    found = identifiers("Alibaba Qwen Releases Qwen-Image-2.1-Turbo, an 8-Step 7B Model in 2026")
    assert "qwen-image-2.1-turbo" in found
    # 纯年份与一两位数字指向的是时间点而不是事件，参与判重会把不同文章拉到一起
    assert "2026" not in found
    assert identifiers("iPhone 18 Pro") == set()
    assert identifiers("something with no version") == set()


def test_same_story_matches_across_languages_via_identifier() -> None:
    """中英文标题用词几乎不重合，模型名是唯一的共同点——这是最可靠的判据。"""
    left = "Alibaba Qwen Releases Qwen-Image-2.1-Turbo, an 8-Step 7B Image Model"
    right = "阿里发布 Qwen-Image-2.1-Turbo：8 步出图的 7B 图像模型"
    assert same_story(left, right) is True
    # 词集相似度达不到转载阈值，说明判定不是靠它——否则中英改写永远判不出来
    assert jaccard(set(_tokens(left)), set(_tokens(right))) < _PARAPHRASE_SIMILARITY


def test_same_story_rejects_shared_generic_words() -> None:
    """共享动作词不算同一件事——否则「X 发布 Y」这一整类标题都会互相吃掉。"""
    assert (
        same_story("Alibaba Qwen Releases Qwen-Image-2.1-Turbo", "JetBrains Releases Mellum2.1: A 12B MoE Model")
        is False
    )
    assert (
        same_story("Chip Industry Week In Review", "Gigabyte BIOS update hints at Intel Raptor Lake refresh") is False
    )


def test_same_story_detects_verbatim_repost() -> None:
    body = "人工智能模型推理成本下降三成，多家厂商跟进降价，行业进入价格战阶段"
    assert same_story(body, body) is True
    assert same_story(body, f"{body} 记者张三报道") is True


def test_same_story_ignores_very_short_texts() -> None:
    """短文本的相似度全是噪声，宁可不判。"""
    assert same_story("AI", "AI 行业周报") is False


def test_keep_indexes_merges_same_story_and_keeps_richest() -> None:
    """同一件事的报道只留一条，且留下的是信息最全的那条。

    注意左边那条虽然来自权重更高的源，但它只有标题——读者从摘要里就能拿到结论，
    所以信息量排在权威度前面（见 `Deduper._quality`）。
    """
    deduper = Deduper()
    items = [
        Item(title="Qwen-Image-2.1-Turbo 发布", url="https://a.example/1", text="", weight=1.2),
        Item(
            title="阿里发布 Qwen-Image-2.1-Turbo",
            url="https://b.example/2",
            text="Qwen-Image-2.1-Turbo 是 8 步出图的 7B 图像模型，已在 ModelScope 开源，支持中文渲染。",
            weight=1.0,
        ),
    ]
    kept = deduper.keep_indexes(items)
    assert kept == [1], "留下信息量更大的那条，而不是只有标题的那条"


def test_keep_indexes_cannot_merge_without_a_shared_identifier() -> None:
    """只有标题、且标题里没有版本号/模型名时判不出同一件事——宁可都保留。

    这不是缺陷而是取舍：判错的代价是读者永远看不到那条来源（见 `same_story`），
    正文层面的同事件合并由 Agent 负责（agent/prompts.py）。
    """
    deduper = Deduper()
    items = [
        Item(title="某公司发布新图像模型", url="https://a.example/1"),
        Item(title="某公司推出图像生成模型", url="https://b.example/2"),
    ]
    assert deduper.keep_indexes(items) == [0, 1]


def test_keep_indexes_keeps_different_news_from_same_source() -> None:
    """同一家媒体的两条不同新闻都要保留——按来源去重是丢失，不是去重。"""
    deduper = Deduper()
    items = [
        Item(title="OpenAI 发布 Decisions API 公测", url="https://tc.example/1", text="支持 12K token 上下文"),
        Item(title="特斯拉在欧洲把 FSD 更名为辅助驾驶", url="https://tc.example/2", text="监管压力下的改名"),
        Item(
            title="Elon Musk 就 Starlink 印度牌照与 Ambani 互怼",
            url="https://tc.example/3",
            text="双方在社交平台上交锋",
        ),
    ]
    assert deduper.keep_indexes(items) == [0, 1, 2]


def test_keep_indexes_prefers_authoritative_source_when_tied() -> None:
    deduper = Deduper()
    body = "某研究团队发布了一项关于推理成本的研究，结论是成本在一年内下降了七成"
    items = [
        Item(title="推理成本一年降七成", url="https://low.example/1", text=body, weight=0.7),
        Item(title="推理成本一年降七成", url="https://high.example/2", text=body, weight=1.2),
    ]
    assert deduper.keep_indexes(items) == [1], "信息量并列时保留更权威的来源"


def test_keep_indexes_preserves_input_order() -> None:
    deduper = Deduper()
    items = [
        Item(title="旧", url="https://a.example/1"),
        Item(title="新", url="https://a.example/2"),
        Item(title="旧", url="https://a.example/1?utm_source=x"),
    ]
    assert deduper.keep_indexes(items) == [0, 1]


def test_keep_indexes_uses_history_loaded_from_db() -> None:
    """历史里的同一件事也要被合并，否则同一话题每天重复推送。"""
    deduper = Deduper()
    deduper.remember(Item(title="阿里发布 Qwen-Image-2.1-Turbo 图像模型", url="https://old.example/1"))
    items = [Item(title="Qwen-Image-2.1-Turbo 开源", url="https://new.example/2", text="8 步出图")]
    assert deduper.keep_indexes(items) == []
