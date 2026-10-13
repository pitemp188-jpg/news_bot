"""
模块: tests.unit.result.test_selection
职责: 校验精选——两次独立打分、同一份规格、按信源分级门槛判定、未评分不入选、评分落库
依赖: result.selection
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import event, select
from tests.fakes.llm import FakeLLM, reply

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.llm import LLMReply
from newsbot.core.models import NewsItem, StoryScore
from newsbot.result import selection as sel


class SnapshotLLM(FakeLLM):
    """记录**深拷贝**的调用入参。

    `FakeLLM` 存的是消息列表对象的引用，后续对同一列表的改动会反映到先前的记录里
    （本项目已因此误判过一次）。要验证"第二遍没有带上第一遍的分数"，必须看的是
    那一刻真正发出去的内容，所以这里拷贝一份。
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMReply:
        result = await super().complete(messages, tools, max_tokens=max_tokens)
        self.calls[-1] = copy.deepcopy(messages)
        return result


def _candidate(item_id: int = 1, *, tier: str = "T2") -> sel.Candidate:
    return sel.Candidate(
        news_item_id=item_id, title="某公司发布新一代芯片", text="官方称性能提升三成", source="量子位", tier=tier
    )


# ── 分数解析 ──


def test_parse_score_accepts_bare_number() -> None:
    assert sel.parse_score("8") == 8


def test_parse_score_reads_the_last_number_line() -> None:
    """格式要求放在提示词末尾，所以要取**最后**一个"整行数字"，不是第一个。"""
    assert sel.parse_score("先看事实：2026 年发布\n8") == 8


def test_parse_score_rejects_out_of_range_year() -> None:
    """年份不能被当成满分：宁可判为未评分，也不要写出 2024 分。"""
    assert sel.parse_score("2024") is None


def test_parse_score_returns_none_for_empty_text() -> None:
    assert sel.parse_score("") is None
    assert sel.parse_score("我无法判断") is None


def test_parse_score_accepts_trailing_unit() -> None:
    assert sel.parse_score("7分") == 7


# ── 信源分级 ──


def test_classify_tier_matches_official_domain() -> None:
    tier = sel.classify_tier("", "https://openai.com/blog/x", official=["openai.com"], quasi=[])
    assert tier == sel.TIER_OFFICIAL


def test_classify_tier_matches_subdomain() -> None:
    tier = sel.classify_tier("", "https://news.openai.com/x", official=["openai.com"], quasi=[])
    assert tier == sel.TIER_OFFICIAL


def test_classify_tier_quasi_official() -> None:
    tier = sel.classify_tier("", "https://weibo.com/x", official=["openai.com"], quasi=["weibo.com"])
    assert tier == sel.TIER_QUASI


def test_classify_tier_unknown_source_is_media() -> None:
    """不认识的来源一律 T2：把陌生来源当官方，等于替读者做了可信度背书。"""
    assert (
        sel.classify_tier("量子位", "https://www.qbitai.com/x", official=["openai.com"], quasi=["weibo.com"])
        == sel.TIER_MEDIA
    )


def test_classify_tier_does_not_match_partial_domain() -> None:
    """`notopenai.com` 不能因为包含 `openai.com` 就被当成官方。"""
    assert sel.classify_tier("", "https://notopenai.com/x", official=["openai.com"], quasi=[]) == sel.TIER_MEDIA


# ── 入选判定 ──


def _score(score1: int | None, score2: int | None, threshold: int = 8, gap: int = 3) -> sel.Score:
    return sel.Score(news_item_id=1, score1=score1, score2=score2, threshold=threshold, unstable_gap=gap)


def test_selected_when_both_passes_meet_threshold() -> None:
    assert _score(8, 8).selected is True


def test_not_selected_when_only_one_pass_meets_threshold() -> None:
    """两次都要够：一次高一次低说明标准在它身上不稳定，取平均会掩盖掉。"""
    assert _score(10, 5).selected is False


def test_unscored_never_selected() -> None:
    """未评分不入选——"全选"正是改造前那份满是副本的日报。"""
    assert _score(None, None).selected is False
    assert _score(10, None).selected is False


def test_threshold_is_per_tier() -> None:
    official = _score(6, 6, threshold=6)
    media = _score(6, 6, threshold=8)
    assert official.selected is True
    assert media.selected is False


def test_unstable_when_two_passes_diverge() -> None:
    assert _score(9, 4).unstable is True
    assert _score(8, 7).unstable is False


def test_unstable_gap_follows_the_given_setting() -> None:
    assert _score(8, 7, gap=3).unstable is False
    assert _score(8, 7, gap=1).unstable is True


def test_unscored_is_not_unstable() -> None:
    assert _score(None, 9).unstable is False


def test_describe_explains_why_it_did_not_make_it() -> None:
    """落库的说明要能回答"为什么它没进"，否则事后无从改进标准。"""
    text = _score(9, 4).describe()
    assert "9/4" in text
    assert "16" in text, "门槛要写出来"
    assert "复核" in text


def test_describe_says_unscored() -> None:
    assert "未评分" in _score(None, None).describe()


# ── 打分流程 ──


async def test_two_passes_use_the_same_prompt() -> None:
    llm = SnapshotLLM(replies=[reply("7"), reply("7")])
    selector = sel.Selector(llm, EditorialSection())
    candidate = _candidate()
    await selector.score(candidate)
    assert len(llm.calls) == 2
    assert llm.calls[0] == llm.calls[1], "第二遍必须用与第一遍完全相同的规格"


async def test_second_pass_carries_no_first_score() -> None:
    """带上第一遍的分数会让第二遍变成确认，差距信号随之归零。"""
    llm = SnapshotLLM(replies=[reply("9"), reply("2")])
    selector = sel.Selector(llm, EditorialSection())
    candidate = _candidate()
    await selector.score(candidate)
    # 第二遍发出去的内容必须与"从头新起一遍"完全一致——不能多出第一遍的分数
    assert llm.calls[1] == selector.build_prompt(candidate)
    # 再逐字核一遍用户消息：除了来源/分级/标题/内容，不该多出任何东西（比如第一遍的分数）
    assert (
        llm.calls[1][1]["content"]
        == "来源：量子位\n信源分级：T2\n\n标题：某公司发布新一代芯片\n内容：某公司发布新一代芯片\n官方称性能提升三成"
    )


async def test_score_records_both_passes() -> None:
    llm = SnapshotLLM(replies=[reply("9"), reply("7")])
    selector = sel.Selector(llm, EditorialSection())
    score, usages = await selector.score(_candidate())
    assert (score.score1, score.score2) == (9, 7)
    assert len(usages) == 2, "两次调用的用量都要记，成本是双份"


async def test_one_failed_pass_marks_unscored_instead_of_substituting() -> None:
    """第一次拿不到分数时，不能用第二次的结果顶替——那样"两次独立打分"就是假的。"""
    llm = SnapshotLLM(replies=[reply(""), reply("9")])
    selector = sel.Selector(llm, EditorialSection())
    score, _ = await selector.score(_candidate())
    assert score.score1 is None
    assert score.score2 == 9
    assert score.scored is False
    assert score.selected is False


async def test_score_respects_the_configured_output_cap() -> None:
    """推理 token 与正文共用额度，上限必须从配置传下去。"""
    llm = SnapshotLLM(replies=[reply("8"), reply("8")])
    config = EditorialSection(score_max_tokens=1234)
    await sel.Selector(llm, config).score(_candidate())
    assert llm.max_tokens_seen == [1234, 1234]


async def test_select_applies_each_candidates_own_threshold() -> None:
    llm = SnapshotLLM(replies=[reply("6"), reply("6"), reply("6"), reply("6")])
    selector = sel.Selector(llm, EditorialSection())
    result = await selector.select([_candidate(1, tier=sel.TIER_OFFICIAL), _candidate(2, tier=sel.TIER_MEDIA)])
    assert [item.news_item_id for item in result.selected] == [1]
    assert [item.news_item_id for item in result.unstable] == []
    assert result.unscored == []


async def test_select_caps_the_batch_and_says_so() -> None:
    """逐条两次调用 = 双倍成本，条数必须有上限（这里验证上限真的生效）。"""
    llm = SnapshotLLM(replies=[reply("8")] * 4)
    selector = sel.Selector(llm, EditorialSection(max_candidates=2))
    result = await selector.select([_candidate(index) for index in range(4)])
    assert len(result.scores) == 2, "只打分前两条"
    assert len(llm.calls) == 4, "两条 × 两次"


async def test_select_reports_unscored_bucket() -> None:
    llm = SnapshotLLM(replies=[reply(""), reply("")])
    result = await sel.Selector(llm, EditorialSection()).select([_candidate()])
    assert len(result.unscored) == 1
    assert result.selected == []


def test_threshold_of_falls_back_to_the_strictest_tier() -> None:
    selector = sel.Selector(FakeLLM(), EditorialSection())
    assert selector.threshold_of("T9") == 8


def test_from_config_wires_the_gap() -> None:
    selector = sel.Selector.from_config(FakeLLM(), EditorialSection(unstable_gap=1))
    assert selector.unstable_gap == 1


# ── 落库 ──


async def _seed_item(db: Database, url: str) -> int:
    async with db.session() as session:
        item = NewsItem(url=url, url_hash=url, title="某公司发布新一代芯片")
        session.add(item)
        await session.commit()
        return item.id


async def test_save_scores_persists_reason_and_selection(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'score.db'}")
    await db.prepare()
    item_id = await _seed_item(db, "https://a.example/1")
    result = sel.Selection(scores=[sel.Score(news_item_id=item_id, score1=9, score2=4, threshold=8)])
    assert await sel.save_scores(db, result) == 1
    async with db.session() as session:
        row = await session.scalar(select(StoryScore).where(StoryScore.news_item_id == item_id))
    assert row is not None
    assert (row.score1, row.score2) == (9, 4)
    assert row.selected is False
    assert "9/4" in row.reason
    await db.dispose()


async def test_save_scores_updates_instead_of_duplicating(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'score2.db'}")
    await db.prepare()
    item_id = await _seed_item(db, "https://a.example/2")
    await sel.save_scores(db, sel.Selection(scores=[sel.Score(news_item_id=item_id, score1=3, score2=3, threshold=8)]))
    await sel.save_scores(db, sel.Selection(scores=[sel.Score(news_item_id=item_id, score1=9, score2=9, threshold=8)]))
    async with db.session() as session:
        rows = (await session.scalars(select(StoryScore))).all()
    assert len(rows) == 1, "同一篇文章只留最后一次评分"
    assert rows[0].selected is True
    await db.dispose()


async def test_save_scores_is_immune_to_concurrent_writers(tmp_path: Any) -> None:
    """另一个写者在"查询到"与"写入"之间插入同一行时，本批评分不能整批失败。

    真实长跑里两个服务实例的 30 分钟定时刷新同时打分，两边都查到"还没有"、再插入时
    撞上 `UNIQUE constraint failed: story_score.news_item_id`，那一批判定就整批丢了
    （日志：`精选打分失败: IntegrityError`，而 `精选完成` 已经打过 24 条）。
    修法是单条原子的 `ON CONFLICT DO UPDATE`。这里用 SQLAlchemy 事件挂钩，在本批的
    写入语句执行前由同一条连接抢插一行，精确落在"查询之后、插入之前"那个窗口：
    读-再-写的实现必然抛 IntegrityError，原子 upsert 则照常覆盖。
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'score4.db'}")
    await db.prepare()
    item_id = await _seed_item(db, "https://a.example/race")

    state = {"intruded": False}
    insert_sql = (
        "INSERT INTO story_score (news_item_id, score1, score2, selected, reason, created_at)"
        " VALUES (?, 1, 1, 0, '另一个实例写的', '2026-01-01 00:00:00')"
    )

    def intrude(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        lowered = statement.lstrip().lower()
        if state["intruded"] or "story_score" not in statement.lower() or not lowered.startswith("insert"):
            return
        state["intruded"] = True
        conn.exec_driver_sql(insert_sql, (item_id,))

    event.listen(db.engine.sync_engine, "before_cursor_execute", intrude)
    try:
        written = await sel.save_scores(
            db, sel.Selection(scores=[sel.Score(news_item_id=item_id, score1=9, score2=9, threshold=8)])
        )
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", intrude)

    assert state["intruded"], "挂钩未生效，竞态没有被复现"
    assert written == 1
    async with db.session() as session:
        rows = (await session.scalars(select(StoryScore))).all()
    assert len(rows) == 1, "不能出现重复评分行"
    assert rows[0].selected is True, "本批次的结果要覆盖抢插进来的那一行"
    await db.dispose()


async def test_save_scores_handles_empty_selection(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'score3.db'}")
    await db.prepare()
    assert await sel.save_scores(db, sel.Selection()) == 0
    await db.dispose()


def test_usage_total_aggregates_calls() -> None:
    result = sel.Selection(usage=[reply(tokens=10).usage(), reply(tokens=5).usage()])
    total = sel.usage_total(result)
    assert total["calls"] == 2
    assert total["prompt_tokens"] == 15
