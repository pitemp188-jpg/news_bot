"""
模块: tests.unit.result.test_digest
职责: 校验日报编排——重要度公式、一个事件一条、本期只有后续要扣分、纯规则不调模型
依赖: result.digest
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.fakes.llm import FakeLLM

from newsbot.core.db import Database
from newsbot.core.models import NewsItem, ReportItem, Story, StoryScore
from newsbot.result import digest as dg
from newsbot.result import hot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_importance_uses_log_for_participants() -> None:
    """参与者数量的贡献是边际递减的：20 家比 10 家只重要一点，不是重要一倍。"""
    assert dg.importance(0, 1) == round(5 * math.log2(2), 3)
    assert dg.importance(0, 2) == round(5 * math.log2(3), 3)
    gap_small = dg.importance(0, 2) - dg.importance(0, 1)
    gap_large = dg.importance(0, 11) - dg.importance(0, 10)
    assert gap_large < gap_small


def test_importance_adds_official_bonus() -> None:
    assert dg.importance(5, 2, official=True) - dg.importance(5, 2) == dg.OFFICIAL_BONUS


def test_importance_penalises_follow_up_only() -> None:
    """读者要先知道发生了什么，本期只有后续进展的事件要排在后面。"""
    gap = dg.importance(5, 2) - dg.importance(5, 2, follow_up_only=True)
    assert abs(gap - dg.FOLLOW_UP_PENALTY) < 1e-9, "先 round 再相减会差 1e-15，比较要留容差"


def test_importance_survives_zero_participants() -> None:
    assert dg.importance(0, 0) == 0.0


def test_periods_map_to_lookback_days() -> None:
    assert dg.PERIOD_DAYS == {"daily": 1, "weekly": 7, "monthly": 30}


def test_title_labels_the_period() -> None:
    digest = dg.Digest(period="weekly", since=NOW, until=NOW)
    assert "周报" in digest.title


def test_render_says_nothing_qualified() -> None:
    digest = dg.Digest(period="daily", since=NOW, until=NOW)
    assert "没有达到入选标准" in digest.render()


def test_entry_line_shows_participant_count_and_flags() -> None:
    entry = dg.Entry(story_id=1, title="某公司发布芯片", participants=4, official=True, follow_up_only=True)
    text = entry.line()
    assert "4 家报道" in text
    assert "官方一手" in text
    assert "后续进展" in text


def test_entry_line_does_not_call_the_model() -> None:
    """组装阶段不调用模型：每条的内容都必须能由数据直接拼出来。"""
    entry = dg.Entry(story_id=1, title="标题", titles=["标题", "另一家标题"], participants=2)
    assert "另一家标题" in entry.line()


def test_within_period_uses_last_seen() -> None:
    """上周开始、今天有进展的事件属于今天；按首次出现筛会让它永远不再出现。"""
    story = Story(title="x", first_seen_at=NOW - timedelta(days=9), last_seen_at=NOW - timedelta(hours=2))
    assert dg.within_period(story, NOW, NOW - timedelta(days=1), NOW) is True


def test_within_period_rejects_old_story() -> None:
    story = Story(title="x", first_seen_at=NOW - timedelta(days=9), last_seen_at=NOW - timedelta(days=3))
    assert dg.within_period(story, NOW, NOW - timedelta(days=1), NOW) is False


# ── 组装 ──


async def _seed(
    db: Database,
    *,
    story_title: str,
    last_seen_hours: float,
    reports: list[tuple[str, str, str, bool]],
    scores: list[int] | None = None,
) -> int:
    """建一个事件与它的报道。`reports` 每项是 (participant, tier, role, editorial)。"""
    async with db.session() as session:
        story = Story(
            title=story_title,
            first_seen_at=NOW - timedelta(hours=last_seen_hours),
            last_seen_at=NOW - timedelta(hours=last_seen_hours),
        )
        session.add(story)
        await session.flush()
        for index, (participant, tier, role, editorial) in enumerate(reports):
            url = f"https://{participant}.example/{story.id}-{index}"
            item = NewsItem(url=url, url_hash=url, title=f"{story_title} - {participant}", source=participant)
            session.add(item)
            await session.flush()
            session.add(
                ReportItem(
                    story_id=story.id,
                    news_item_id=item.id,
                    participant=participant,
                    tier=tier,
                    role=role,
                    editorial=editorial,
                    observed_at=NOW - timedelta(hours=last_seen_hours - index / 10),
                )
            )
            if scores:
                total = scores[index % len(scores)]
                session.add(StoryScore(news_item_id=item.id, score1=total, score2=total, selected=True))
        await session.commit()
        return story.id


async def test_assemble_puts_one_entry_per_story(tmp_path: Any) -> None:
    """一个事件一条：组内多家报道合并成一条，不是列出多份副本。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest.db'}")
    await db.prepare()
    await _seed(
        db,
        story_title="某公司发布芯片",
        last_seen_hours=1,
        reports=[("qbitai", "T2", "primary", True), ("infoq", "T2", "duplicate", True)],
    )
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert len(digest.entries) == 1
    assert digest.entries[0].participants == 2
    await db.dispose()


async def test_assemble_skips_stories_without_fresh_reports(tmp_path: Any) -> None:
    """本期没有新报道 = 旧闻，不进本期。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest2.db'}")
    await db.prepare()
    await _seed(db, story_title="三天前的事了", last_seen_hours=60, reports=[("qbitai", "T2", "primary", True)])
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert digest.entries == [], "本期没有新报道的旧事件不进本期"
    await db.dispose()


async def test_assemble_sorts_by_importance(tmp_path: Any) -> None:
    """官方一手 + 更多参与者的排前面。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest3.db'}")
    await db.prepare()
    await _seed(
        db, story_title="小众消息", last_seen_hours=2, reports=[("a.example", "T2", "primary", True)], scores=[5]
    )
    await _seed(
        db,
        story_title="官方大新闻",
        last_seen_hours=1,
        reports=[
            ("official.example", "T1", "primary", True),
            ("b.example", "T2", "duplicate", True),
            ("c.example", "T2", "duplicate", True),
        ],
        scores=[9],
    )
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert next(entry.title for entry in digest.entries) == "官方大新闻"
    await db.dispose()


async def test_assemble_penalises_follow_up_only_events(tmp_path: Any) -> None:
    """本期只有后续进展的事件排在有一手报道的后面（同等其他条件）。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest4.db'}")
    await db.prepare()
    await _seed(
        db,
        story_title="后续消息",
        last_seen_hours=1,
        reports=[("a.example", "T2", "follow_up", True), ("b.example", "T2", "duplicate", True)],
        scores=[8],
    )
    await _seed(
        db,
        story_title="一手消息",
        last_seen_hours=1,
        reports=[("c.example", "T2", "primary", True), ("d.example", "T2", "duplicate", True)],
        scores=[8],
    )
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert [entry.title for entry in digest.entries] == ["一手消息", "后续消息"]
    await db.dispose()


async def test_assemble_marks_follow_up_only(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest5.db'}")
    await db.prepare()
    await _seed(db, story_title="后续消息", last_seen_hours=1, reports=[("a.example", "T2", "follow_up", True)])
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert digest.entries[0].follow_up_only is True
    await db.dispose()


async def test_assemble_is_pure_rules(tmp_path: Any, monkeypatch: Any) -> None:
    """组装阶段一次模型都不调——重要度必须可复算，否则同一批数据两次跑出两个排序。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest6.db'}")
    await db.prepare()
    await _seed(db, story_title="某公司发布芯片", last_seen_hours=1, reports=[("qbitai", "T2", "primary", True)])

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("编排阶段不该调用模型")

    monkeypatch.setattr(FakeLLM, "complete", boom)
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert len(digest.entries) == 1
    await db.dispose()


async def test_assemble_respects_the_limit(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest7.db'}")
    await db.prepare()
    for index in range(4):
        await _seed(
            db,
            story_title=f"事件 {index}",
            last_seen_hours=1,
            reports=[(f"s{index}.example", "T2", "primary", True)],
            scores=[index],
        )
    digest = await dg.assemble(db, period="daily", now=NOW, limit=2)
    assert len(digest.entries) == 2
    await db.dispose()


async def test_assemble_on_empty_db(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest8.db'}")
    await db.prepare()
    digest = await dg.assemble(db, period="weekly", now=NOW)
    assert digest.entries == []
    assert digest.period == "weekly"
    await db.dispose()


async def test_assemble_ignores_community_only_for_official_flag(tmp_path: Any) -> None:
    """社区讨论算参与者，但不能让事件被当成"官方一手"。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest9.db'}")
    await db.prepare()
    await _seed(
        db,
        story_title="社区热议",
        last_seen_hours=1,
        reports=[("news.ycombinator.com", "T2", "primary", False), ("lobste.rs", "T2", "duplicate", False)],
    )
    digest = await dg.assemble(db, period="daily", now=NOW)
    assert digest.entries[0].official is False
    assert digest.entries[0].participants == 2
    await db.dispose()


async def test_report_render_lists_entries(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'digest10.db'}")
    await db.prepare()
    await _seed(db, story_title="某公司发布芯片", last_seen_hours=1, reports=[("qbitai", "T2", "primary", True)])
    digest = await dg.assemble(db, period="daily", now=NOW)
    text = digest.render()
    assert "某公司发布芯片" in text
    assert "日报" in text
    await db.dispose()


def test_decorate_recomputes_importance_from_heat() -> None:
    """热度是全网口径，日报排序要用它修正参与者数量。"""
    entry = dg.Entry(story_id=1, title="x", participants=1, importance=dg.importance(0, 1))
    rows = [
        hot.HotEntry(
            story_id=1,
            title="x",
            heat=3.5,
            index=35.0,
            participants=5,
            sources=2,
            trend="flat",
            trend_pct=None,
            badges=[],
            latest_at=None,
            first_at=None,
            participant_names=[],
        )
    ]
    dg.decorate([entry], rows)
    assert entry.participants == 5
    assert entry.heat == 3.5
    assert entry.importance == dg.importance(0, 5)


def test_decorate_leaves_stories_outside_the_ranking_alone() -> None:
    entry = dg.Entry(story_id=2, title="x", participants=1)
    dg.decorate([entry], [])
    assert entry.participants == 1
    assert entry.importance == dg.importance(0, 1)
