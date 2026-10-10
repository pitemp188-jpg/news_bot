"""
模块: tests.unit.result.test_dedup
职责: 校验 URL 归一化、内容指纹、simhash 近似去重与历史指纹载入
依赖: result.dedup
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fakes.llm import FakeLLM, reply

from newsbot.core.db import Database
from newsbot.core.models import NewsItem
from newsbot.result.dedup import (
    _GROUP_MAX_TOKENS,
    _PARAPHRASE_SIMILARITY,
    Deduper,
    Item,
    StoryGrouper,
    _tokens,
    content_digest,
    hamming,
    identifiers,
    is_digest,
    jaccard,
    normalize_url,
    parse_groups,
    same_group,
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


def test_richness_is_not_biased_towards_english_length() -> None:
    """同样信息量下英文字符数天然更多，信息量按词数比才不会偏向英文源。

    实测踩到：一条中文标题 + 完整摘要被一条只有标题的英文报道顶掉，只因为英文
    那一串字符更长。
    """
    chinese = Item(
        title="Musk 与 Ambani 就 Starlink 印度牌照公开互怼",
        url="https://cn.example/1",
        text="两人在社交平台上互相指责，焦点是落地许可与频谱分配。",
    )
    english = Item(
        title="Elon Musk intensifies attack on Ambani over Starlink India licence",
        url="https://en.example/2",
    )
    assert len(chinese.body) > len(english.body) or chinese.richness >= english.richness
    assert chinese.richness > english.richness, "带摘要的中文报道信息量更大"


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
    assert deduper.keep_indexes(items).kept == [1], "留下信息量更大的那条，而不是只有标题的那条"


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
    assert deduper.keep_indexes(items).kept == [0, 1]


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
    assert deduper.keep_indexes(items).kept == [0, 1, 2]


def test_keep_indexes_prefers_authoritative_source_when_tied() -> None:
    deduper = Deduper()
    body = "某研究团队发布了一项关于推理成本的研究，结论是成本在一年内下降了七成"
    items = [
        Item(title="推理成本一年降七成", url="https://low.example/1", text=body, weight=0.7),
        Item(title="推理成本一年降七成", url="https://high.example/2", text=body, weight=1.2),
    ]
    assert deduper.keep_indexes(items).kept == [1], "信息量并列时保留更权威的来源"


def test_keep_indexes_preserves_input_order() -> None:
    deduper = Deduper()
    items = [
        Item(title="旧", url="https://a.example/1"),
        Item(title="新", url="https://a.example/2"),
        Item(title="旧", url="https://a.example/1?utm_source=x"),
    ]
    assert deduper.keep_indexes(items).kept == [0, 1]


def test_keep_indexes_uses_history_loaded_from_db() -> None:
    """历史里的同一件事也要被合并，否则同一话题每天重复推送。"""
    deduper = Deduper()
    deduper.remember(Item(title="阿里发布 Qwen-Image-2.1-Turbo 图像模型", url="https://old.example/1"))
    items = [Item(title="Qwen-Image-2.1-Turbo 开源", url="https://new.example/2", text="8 步出图")]
    assert deduper.keep_indexes(items).kept == []


# ── 语义分组：模型只判"是不是同一件事"，取舍仍由确定性规则决定 ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3,7,12\n5,9", {2: 0, 6: 0, 11: 0, 4: 1, 8: 1}),
        # 编号用中文逗号、顿号、空格混合也要能解析
        ("3，7、12", {2: 0, 6: 0, 11: 0}),
        # 说明文字、代码块标记、表头一律忽略
        ("```\n分组如下：\n3,7\n```", {2: 0, 6: 0}),
        ("无", {}),
        ("", {}),
        # 只有一个编号的行不成组
        ("4,4\n7", {}),
        # 越界编号丢掉，剩下的仍成组
        ("3,7,999\n1", {2: 0, 6: 0}),
        # 一个编号出现在多组时并成一组（丢哪组都可能漏合并）
        ("1,2\n2,3", {0: 0, 1: 0, 2: 0}),
    ],
)
def test_parse_groups_is_forgiving(text: str, expected: dict[int, int]) -> None:
    assert parse_groups(text, total=12) == expected


def test_same_group_ignores_missing_tags() -> None:
    groups = {0: 0, 2: 0, 1: 1}
    assert same_group(0, 2, groups) is True
    assert same_group(0, 1, groups) is False
    assert same_group(0, 3, groups) is False
    assert same_group(0, 2, None) is False
    assert same_group(0, 2, {}) is False


async def test_grouped_items_are_merged_even_without_shared_identifier() -> None:
    """这正是词法判据的盲区：中文改写、没有版本号可对齐。

    调用方按提示词要求写"6 条以上才调用"，所以这里给足条目数。
    """
    items = [
        Item(
            title="Musk 与 Ambani 就 Starlink 印度牌照公开互怼",
            url="https://cn.example/1",
            text="两人在社交平台上互相指责，焦点是 Starlink 在印度的落地许可与频谱分配。",
        ),
        Item(title="Elon Musk intensifies attack on Ambani over Starlink India licence", url="https://en.example/2"),
        Item(title="某公司发布新一代推理芯片", url="https://a.example/3"),
        Item(title="某云厂商下调对象存储价格", url="https://b.example/4"),
        Item(title="开源模型社区新增两个多模态模型", url="https://c.example/5"),
        Item(title="某车企公布三季度交付量", url="https://d.example/6"),
    ]
    # 先确认词法判据确实抓不到这两条
    assert same_story(items[0].body, items[1].body) is False

    grouper = StoryGrouper(FakeLLM(replies=[reply("1,2")]))
    deduper = Deduper(grouper=grouper)
    kept = await deduper.keep(items)
    assert kept == [0, 2, 3, 4, 5], "同一件事只留一条，且留下的是信息量更大的那条"


async def test_grouper_only_decides_merging_not_selection() -> None:
    """模型不能左右"保留哪一条"：即使它把信息量小的排在前面，留下的仍是信息量大的。"""
    items = [
        Item(title="小事一桩", url="https://a.example/1"),
        Item(title="同一件事的详细报道", url="https://b.example/2", text="补充了背景、数据与影响"),
        Item(title="无关新闻", url="https://c.example/3"),
        Item(title="另一条无关新闻", url="https://d.example/4"),
    ]
    grouper = StoryGrouper(FakeLLM(replies=[reply("1,2")]), min_items=4)
    deduper = Deduper(grouper=grouper)
    assert await deduper.keep(items) == [1, 2, 3]


async def test_grouper_is_skipped_when_there_are_too_few_items() -> None:
    """一两条不值得付一次模型调用。"""
    llm = FakeLLM(replies=[reply("1,2")])
    deduper = Deduper(grouper=StoryGrouper(llm, min_items=4))
    await deduper.keep([Item(title="a", url="https://a/1"), Item(title="b", url="https://b/2")])
    assert llm.calls == [], "条数不足时不该调用模型"


async def test_grouper_is_skipped_when_there_are_too_many_items() -> None:
    """条数过多时上下文会超预算，宁可不做语义分组。"""
    llm = FakeLLM(replies=[reply("1,2")])
    deduper = Deduper(grouper=StoryGrouper(llm, max_items=3))
    items = [Item(title=f"t{index}", url=f"https://a/{index}") for index in range(5)]
    assert await deduper.keep(items) == [0, 1, 2, 3, 4]
    assert llm.calls == []


@pytest.mark.parametrize(
    ("raw", "label"),
    [("", "空回复"), ("我看不出有什么重复", "无法解析"), ("999,1000", "编号越界"), ("1,1", "只有一个有效编号")],
)
async def test_grouper_failure_keeps_every_source(raw: str, label: str) -> None:
    """模型抽风不能丢来源：解析不出分组就退回词法判据（这里是全部保留）。"""
    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    deduper = Deduper(grouper=StoryGrouper(FakeLLM(replies=[reply(raw)])))
    assert await deduper.keep(items) == [0, 1, 2, 3], label


async def test_grouper_exception_falls_back_to_lexical() -> None:
    class Boom:
        async def complete(self, messages: object, tools: object = None) -> object:
            raise RuntimeError("模型挂了")

        async def aclose(self) -> None:
            return None

    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    deduper = Deduper(grouper=StoryGrouper(Boom()))  # type: ignore[arg-type]
    assert await deduper.keep(items) == [0, 1, 2, 3]


async def test_grouper_records_usage_for_cost_stats() -> None:
    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    deduper = Deduper(grouper=StoryGrouper(FakeLLM(replies=[reply("1,2", tokens=7)])))
    await deduper.keep(items)
    assert len(deduper.usages) == 1
    assert deduper.usages[0].prompt_tokens == 7


async def test_no_grouper_means_no_llm_call_and_no_usage() -> None:
    deduper = Deduper()
    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    assert await deduper.keep(items) == [0, 1, 2, 3]
    assert deduper.usages == []


async def test_grouper_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class Slow:
        async def complete(self, messages: object, tools: object = None) -> object:
            await asyncio.sleep(5)
            return reply("1,2")

        async def aclose(self) -> None:
            return None

    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    deduper = Deduper(grouper=StoryGrouper(Slow(), timeout=0.05))  # type: ignore[arg-type]
    assert await deduper.keep(items) == [0, 1, 2, 3]


async def test_grouper_caps_output_length() -> None:
    """限长是延迟与成本的根治手段：不限长时实测一次分组用掉 2588 个 completion token。"""
    llm = FakeLLM(replies=[reply("1,2")])
    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    await Deduper(grouper=StoryGrouper(llm)).keep(items)
    assert llm.max_tokens_seen == [_GROUP_MAX_TOKENS]


async def test_grouper_prompt_puts_format_requirement_last() -> None:
    """格式要求放在末尾：写在前面时模型倾向先写分析再给结论（实测）。"""
    items = [Item(title=f"不同的事 {index}", url=f"https://a/{index}") for index in range(4)]
    prompt = StoryGrouper(FakeLLM()).build_prompt(items)
    assert "不要输出任何解释" in prompt.strip().rsplit("\n", 1)[-1], "格式要求必须在最后一段"
    assert "4. 不同的事 3" in prompt


async def test_grouper_prompt_carries_titles_and_snippets() -> None:
    llm = FakeLLM(replies=[reply("1,2")])
    items = [
        Item(title="标题甲", url="https://a/1", text="摘要甲"),
        Item(title="标题乙", url="https://b/2"),
        Item(title="标题丙", url="https://c/3"),
        Item(title="标题丁", url="https://d/4"),
    ]
    await Deduper(grouper=StoryGrouper(llm, min_items=4)).keep(items)
    prompt = llm.calls[0][0]["content"]
    assert "标题甲 | 摘要甲" in prompt
    assert "标题丙" in prompt, "每条都要带上编号与内容，模型才能按编号分组"
    assert "4." in prompt


async def test_digest_items_are_never_merged_away() -> None:
    """摘要型条目是容器：早报因提到某条新闻就与它合并，会连带丢掉其余几条（实测）。

    ifanr 的「早报｜苹果定档…/三星减产…/保时捷裁员 9000 个岗位」因为提到 Manus
    融资，被判定与「Manus 成功融资逾 5 亿美元」是同一件事而合并掉，另外四条新闻
    跟着消失。
    """
    items = [
        Item(title="Manus 成功融资逾 5 亿美元", url="https://a.example/1", text="母公司蝴蝶效应宣布融资消息。"),
        Item(
            title="早报｜苹果定档10月13日发布新品/三星手机业务或减产30%/保时捷计划削减9000个岗位",
            url="https://a.example/2",
            text="· Manus 母公司完成超 5 亿美元新一轮融资 · 小鹏 Robotaxi 定名「小鹏悠游」",
        ),
        Item(title="某云厂商下调对象存储价格", url="https://b.example/3"),
        Item(title="开源模型社区新增两个多模态模型", url="https://c.example/4"),
    ]
    # 模型即使把两条判成同一件事，摘要型条目也不能被合并
    deduper = Deduper(grouper=StoryGrouper(FakeLLM(replies=[reply("1,2")]), min_items=4))
    assert await deduper.keep(items) == [0, 1, 2, 3]


async def test_digest_item_is_not_matched_against_history() -> None:
    """历史里恰好有一条与早报提到的新闻相同，不代表整份早报已经推送过。"""
    deduper = Deduper()
    deduper.remember(Item(title="Manus 成功融资逾 5 亿美元", url="https://old.example/1"))
    digest = Item(
        title="早报｜Manus 母公司完成超 5 亿美元融资/苹果定档10月13日",
        url="https://a.example/2",
        text="· Manus 母公司完成超 5 亿美元新一轮融资 · 小鹏 Robotaxi 定名「小鹏悠游」",
    )
    assert deduper.is_duplicate(digest) is False


def test_digest_detection_covers_common_forms() -> None:
    assert is_digest(Item(title="早报｜苹果定档10月13日", url="https://a/1")) is True
    assert is_digest(Item(title="TechCrunch Daily Digest", url="https://a/2")) is True
    assert is_digest(Item(title="一周 AI 盘点", url="https://a/3")) is True
    assert is_digest(Item(title="Manus 成功融资逾 5 亿美元", url="https://a/4")) is False


async def test_grouper_prompt_excludes_digest_items() -> None:
    """摘要型条目不该出现在提示词里，否则模型会把它和它提到的新闻归为一组。"""
    llm = FakeLLM(replies=[reply("1,2")])
    items = [
        Item(title="普通新闻甲", url="https://a/1"),
        Item(title="早报｜甲乙丙", url="https://a/2"),
        Item(title="普通新闻乙", url="https://a/3"),
        Item(title="普通新闻丙", url="https://a/4"),
    ]
    await Deduper(grouper=StoryGrouper(llm, min_items=3)).keep(items)
    prompt = llm.calls[0][0]["content"]
    assert "早报｜甲乙丙" not in prompt
    assert "普通新闻甲" in prompt


async def test_grouper_maps_positions_back_to_indexes() -> None:
    """分组用的是"候选列表里的位置"，必须还原成原列表下标，否则会合并错条目。

    候选列表是 [1,2,3]（下标 0 是摘要型条目），模型说"第 1、2 条"→ 原下标 1、2；
    两条信息量相当，按下标顺序保留 1、丢掉 2。若没做还原，被丢掉的会是下标 1，
    结果就成了 [0,2,3]。
    """
    llm = FakeLLM(replies=[reply("1,2")])
    items = [
        Item(title="早报｜甲乙丙", url="https://a/1"),
        Item(title="第一条普通新闻", url="https://a/2"),
        Item(title="第二条普通新闻", url="https://a/3"),
        Item(title="第三条普通新闻", url="https://a/4"),
    ]
    kept = await Deduper(grouper=StoryGrouper(llm, min_items=3)).keep(items)
    assert kept == [0, 1, 3]
