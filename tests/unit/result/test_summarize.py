"""
模块: tests.unit.result.test_summarize
职责: 校验事件综述——只为多来源事件写、过期才重写、清洗与容错
依赖: result.summarize
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from tests.fakes.llm import FakeLLM, reply

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.models import NewsItem, ReportItem, Story
from newsbot.result import summarize as sm

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


async def _make_db(tmp_path: Any) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}")
    await db.prepare()
    return db


async def _seed(
    db: Database,
    *,
    title: str,
    url: str,
    source: str = "qbitai.com",
    story_id: int | None = None,
    hours: float = 1,
    minutes: float = 0,
) -> tuple[int, int]:
    """建一条资讯并归入事件（不给 story_id 就新建一个）。返回 (story_id, news_item_id)。"""
    moment = NOW - timedelta(hours=hours, minutes=minutes)
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
        if story_id is None:
            story = Story(title=title, first_seen_at=moment, last_seen_at=moment)
            session.add(story)
            await session.flush()
            story_id = story.id
        session.add(
            ReportItem(
                story_id=story_id,
                news_item_id=item.id,
                participant=source,
                tier="T2",
                role="primary",
                editorial=True,
                observed_at=moment,
            )
        )
        await session.commit()
        return story_id, item.id


# ── 输出清洗 ──


def test_parse_digest_strips_prefixes_and_quotes() -> None:
    """实测模型即使被告知"直接输出正文"也会带上 `综述：` 或整句引号。"""
    assert sm.parse_digest("综述：某公司发布新一代推理芯片，性能提升三成") == "某公司发布新一代推理芯片，性能提升三成"
    assert sm.parse_digest("摘要：「某公司发布新一代推理芯片」") == "某公司发布新一代推理芯片"
    assert sm.parse_digest('事件综述: "某公司发布新一代推理芯片"') == "某公司发布新一代推理芯片"


def test_parse_digest_collapses_lines() -> None:
    """日报是逐行版面，正文里的换行会打乱缩进。"""
    assert sm.parse_digest("第一句说的是某公司发布了芯片。\n第二句说的是已开始出货。") == (
        "第一句说的是某公司发布了芯片。 第二句说的是已开始出货。"
    )
    assert sm.parse_digest("  多余   空格   出现在   这里  ") == "多余 空格 出现在 这里"


def test_parse_digest_truncates_overlong_text() -> None:
    text = sm.parse_digest("很长的综述。" * 60)
    assert len(text) <= sm.MAX_DIGEST_CHARS + 1, "超长要截断，不能让它扭曲日报版面"
    assert text.endswith("…")


def test_parse_digest_rejects_empty_or_tiny() -> None:
    """取不到可用内容要**如实返回空串**，由调用方按"没写成"处理。

    这里必须返回空串而不是兜底成标题：兜底会让"模型没写出来"被伪装成"写出来了"，
    而日报正是靠"综述为空"来回退到标题列表的。
    """
    assert sm.parse_digest("") == ""
    assert sm.parse_digest("   \n  ") == ""
    assert sm.parse_digest("综述：") == ""
    assert sm.parse_digest("嗯。") == ""


# ── 素材挑选 ──


def _record(title: str, *, hours: float, body: str = "", source: str = "a.com") -> Any:
    from newsbot.core.models import NewsItem as Item
    from newsbot.core.models import ReportItem as Report

    moment = NOW - timedelta(hours=hours)
    return (
        Report(story_id=1, news_item_id=1, participant=source, observed_at=moment),
        Item(url="u", url_hash="u", title=title, summary=body, source=source),
    )


def test_materials_keep_earliest_and_latest() -> None:
    """给太多报道时模型会按顺序复述、写成流水账，所以只给最早与最新。

    "这是什么"由最早那条定调，"最新到哪了"由最新那条定，中间过程没有增量。
    """
    records = [_record(f"第{i}条", hours=10 - i) for i in range(6)]
    picked = [item.title for item in sm.materials_of(records)]
    assert picked[0] == "第0条", "最早的一条要在里面"
    assert picked[-1] == "第5条", "最新的一条要在里面"
    assert len(picked) <= sm.MAX_REPORTS_IN_PROMPT


def test_materials_gives_everything_when_few() -> None:
    records = [_record("甲", hours=3), _record("乙", hours=1)]
    assert [item.title for item in sm.materials_of(records)] == ["甲", "乙"]


def test_materials_drop_duplicate_and_empty_titles() -> None:
    """同一标题出现两次（同一篇文章被两个来源转）只给一份，空标题直接丢掉。"""
    records = [_record("同一个标题", hours=3), _record("同一个标题", hours=1), _record("  ", hours=2)]
    assert [item.title for item in sm.materials_of(records)] == ["同一个标题"]


def test_materials_of_empty_records() -> None:
    assert sm.materials_of([]) == []


# ── 提示词 ──


def test_prompt_carries_title_materials_and_tail_requirement() -> None:
    """格式要求放**末尾**：放前面时模型会先写一段分析，而推理与正文共用输出额度。"""
    prompt = sm.build_prompt("某公司发布芯片", [sm.Material(title="甲来源这样报", source="a.com", observed_at=NOW)])
    assert prompt[0]["role"] == "system"
    user = prompt[1]["content"]
    assert "某公司发布芯片" in user
    assert "甲来源这样报" in user
    assert "a.com" in user
    assert user.rstrip().endswith("请写出这个事件的综述。"), "格式要求必须在最后"
    assert "不要推测" in prompt[0]["content"] or "不要补充背景知识" in prompt[0]["content"]


def test_prompt_truncates_long_body() -> None:
    prompt = sm.build_prompt("标题", [sm.Material(title="甲", body="很长的正文。" * 200, observed_at=NOW)])
    assert len(prompt[1]["content"]) < sm.BODY_CHARS * 3, "正文摘要要给得克制，否则只是在烧推理 token"


# ── 该不该写 ──


async def test_pending_skips_single_source_stories(tmp_path: Any) -> None:
    """独家事件不写综述：它唯一那条报道就是全部内容，标题已经把事说清楚了。

    实测这类条目的"综述"就是把标题重复一遍——纯噪声，还白付一次模型调用。
    """
    db = await _make_db(tmp_path)
    try:
        await _seed(db, title="只有一家报的事", url="https://a.example/1")
        pending = await sm.Summarizer(FakeLLM()).pending(db)
        assert pending.items == []
        assert pending.single == 1, "跳过原因要如实记下来，否则日志会打成「跳过 0」"
    finally:
        await db.dispose()


async def test_pending_includes_multi_source_without_digest(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="两家都报的事（另一家）", url="https://b.example/1", source="b.com", story_id=story_id)
        pending = await sm.Summarizer(FakeLLM()).pending(db)
        assert len(pending.items) == 1
        story, records, covered = pending.items[0]
        assert story.id == story_id
        assert len(records) == 2
        assert covered == 0
    finally:
        await db.dispose()


async def test_pending_skips_fresh_digest(tmp_path: Any) -> None:
    """综述没过期就不重写：同一件事的措辞不该每半小时变一次，也不该白花钱。"""
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="另一家的说法", url="https://b.example/1", source="b.com", story_id=story_id)
        async with db.session() as session:
            story = await session.get(Story, story_id)
            assert story is not None
            story.digest = "已写好的综述"
            story.digest_reports = 2
            await session.commit()
        pending = await sm.Summarizer(FakeLLM()).pending(db)
        assert pending.items == []
        assert pending.fresh == 1, "未过期也要计数，否则与「跳过逻辑没生效」无从区分"
    finally:
        await db.dispose()


async def test_pending_rewrites_when_new_reports_arrive(tmp_path: Any) -> None:
    """又有新报道进来时综述过期了，要重写。

    这是 `digest_reports` 存在的理由：不存这个数就只剩两种坏选择——永不更新（长期事件
    永远描述着最初那两家），或每次刷新都重写（白花钱且措辞反复变）。
    """
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="另一家的说法", url="https://b.example/1", source="b.com", story_id=story_id)
        async with db.session() as session:
            story = await session.get(Story, story_id)
            assert story is not None
            story.digest = "只覆盖了两条的综述"
            story.digest_reports = 2
            await session.commit()
        await _seed(db, title="第三家也来了", url="https://c.example/1", source="c.com", story_id=story_id)

        pending = await sm.Summarizer(FakeLLM()).pending(db)
        assert len(pending.items) == 1, "出现了第 3 条报道，综述应判为过期"
        assert pending.items[0][2] == 2, "要告诉调用方上次只覆盖了 2 条"
    finally:
        await db.dispose()


async def test_pending_limit_keeps_the_biggest_gap(tmp_path: Any) -> None:
    """超上限时先写"新报道攒得最多"的那些，而不是按 id 碰运气。"""
    db = await _make_db(tmp_path)
    try:
        small, _ = await _seed(db, title="只多了一条", url="https://a.example/1", source="a.com")
        await _seed(db, title="只多了一条的另一家", url="https://b.example/1", source="b.com", story_id=small)
        async with db.session() as session:
            story = await session.get(Story, small)
            assert story is not None
            story.digest, story.digest_reports = "旧综述", 1
            await session.commit()
        await _seed(db, title="只多了一条的第三家", url="https://c.example/1", source="c.com", story_id=small)

        big, _ = await _seed(db, title="攒了很多条", url="https://d.example/1", source="d.com")
        async with db.session() as session:
            story = await session.get(Story, big)
            assert story is not None
            story.digest, story.digest_reports = "旧综述", 1
            await session.commit()
        for index in range(4):
            await _seed(
                db,
                title=f"攒了很多条第{index}家",
                url=f"https://e{index}.example/1",
                source=f"e{index}.com",
                story_id=big,
            )

        pending = await sm.Summarizer(FakeLLM()).pending(db, limit=1)
        assert len(pending.items) == 1
        assert pending.items[0][0].id == big, "缺口最大的那个事件要先写"
    finally:
        await db.dispose()


# ── 写与容错 ──


async def test_refresh_writes_digest_and_marks_coverage(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="另一家的说法", url="https://b.example/1", source="b.com", story_id=story_id)
        llm = FakeLLM(replies=[reply("某公司发布新一代推理芯片，性能提升三成，已开始出货")])
        result = await sm.Summarizer.from_config(llm, EditorialSection()).refresh(db)
        assert result.written == 1
        assert result.failed == 0
        assert len(result.usage) == 1, "用量要记下来，便于接进成本统计"
        async with db.session() as session:
            story = await session.get(Story, story_id)
        assert story is not None
        assert story.digest == "某公司发布新一代推理芯片，性能提升三成，已开始出货"
        assert story.digest_reports == 2, "要把覆盖的条数记下来，否则过期判定无从下手"
    finally:
        await db.dispose()


async def test_refresh_counts_empty_content_as_failure(tmp_path: Any) -> None:
    """模型返回空正文时**不写库**。

    这个端点是推理模型，推理与正文共用输出额度；上限不足时调用"成功"而正文为空。
    把空串写进去会把"没写出来"伪装成"写出来了"，而日报正是靠"综述为空"回退到标题列表。
    """
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="另一家", url="https://b.example/1", source="b.com", story_id=story_id)
        result = await sm.Summarizer(FakeLLM(replies=[reply("")])).refresh(db)
        assert result.written == 0
        assert result.failed == 1
        async with db.session() as session:
            story = await session.get(Story, story_id)
        assert story is not None and not story.digest
        assert story.digest_reports == 0, "没写成就不该记覆盖条数，否则会被当成「已覆盖」"
    finally:
        await db.dispose()


async def test_refresh_survives_one_failure(tmp_path: Any) -> None:
    """单个事件失败不影响其余——顺序处理时这是最容易被整批打断的地方。"""
    db = await _make_db(tmp_path)
    try:
        first, _ = await _seed(db, title="甲事件", url="https://a.example/1", source="a.com")
        await _seed(db, title="甲事件另一家", url="https://b.example/1", source="b.com", story_id=first)
        second, _ = await _seed(db, title="乙事件", url="https://c.example/1", source="c.com")
        await _seed(db, title="乙事件另一家", url="https://d.example/1", source="d.com", story_id=second)

        llm = FakeLLM(replies=[RuntimeError("模型挂了"), reply("乙事件的综述写得很好")])
        result = await sm.Summarizer(llm).refresh(db)
        assert result.written == 1 and result.failed == 1
        async with db.session() as session:
            rows = (await session.scalars(select(Story).order_by(Story.id))).all()
        assert rows[0].digest is None
        assert rows[1].digest == "乙事件的综述写得很好"
    finally:
        await db.dispose()


async def test_refresh_uses_configured_limits(tmp_path: Any) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id, _ = await _seed(db, title="两家都报的事", url="https://a.example/1", source="a.com")
        await _seed(db, title="另一家", url="https://b.example/1", source="b.com", story_id=story_id)
        config = EditorialSection(summary_max_tokens=1234, summary_timeout_seconds=7.5)
        llm = FakeLLM(replies=[reply("一段写得挺好的事件综述")])
        await sm.Summarizer.from_config(llm, config).refresh(db)
        assert llm.max_tokens_seen == [1234], "输出上限要从配置取"
    finally:
        await db.dispose()


async def test_save_digest_tolerates_missing_story(tmp_path: Any) -> None:
    """并发下事件可能刚被 `--reset` 清掉，此时不能抛异常。"""
    db = await _make_db(tmp_path)
    try:
        await sm.save_digest(db, 99999, "某件事的综述", 2)  # 不抛就算过
    finally:
        await db.dispose()


def test_result_line_mentions_every_counter() -> None:
    result = sm.SummaryResult(written=3, skipped_fresh=5, skipped_single=40, failed=1)
    assert all(part in result.line() for part in ("3", "5", "40", "1"))
