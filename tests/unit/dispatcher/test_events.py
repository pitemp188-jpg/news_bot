"""
模块: tests.unit.dispatcher.test_events
职责: 校验事件层刷新——聚簇 → 打分 → 快照的顺序、待打分筛选、重建与容错
依赖: dispatcher.events
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from tests.fakes.llm import FakeLLM, reply

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.models import HotSnapshot, NewsItem, ReportItem, Story, StoryScore
from newsbot.dispatcher import events as ev

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


async def _seed(
    db: Database,
    *,
    title: str,
    url: str,
    source: str = "qbitai.com",
    assigned: bool = True,
    editorial: bool = True,
    tier: str = "T2",
    locked: bool = False,
    scored: bool = False,
    hours: float = 1,
) -> tuple[int, int]:
    """建一条资讯，可选地归入一个事件。返回 (news_item_id, story_id)。"""
    async with db.session() as session:
        item = NewsItem(
            url=url,
            url_hash=url,
            title=title,
            source=source,
            summary="官方称性能提升三成",
            published_at=NOW - timedelta(hours=hours),
            fetched_at=NOW - timedelta(hours=hours),
        )
        session.add(item)
        await session.flush()
        story = Story(
            title=title,
            first_seen_at=NOW - timedelta(hours=hours),
            last_seen_at=NOW - timedelta(hours=hours),
            locked=locked,
        )
        session.add(story)
        await session.flush()
        if assigned:
            session.add(
                ReportItem(
                    story_id=story.id,
                    news_item_id=item.id,
                    participant=source,
                    tier=tier,
                    role="primary",
                    editorial=editorial,
                    observed_at=NOW - timedelta(hours=hours),
                )
            )
        if scored:
            session.add(StoryScore(news_item_id=item.id, score1=8, score2=8, selected=True))
        await session.commit()
        return item.id, story.id


async def _count(db: Database, model: Any) -> int:
    async with db.session() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ── 待打分筛选 ──


async def test_pending_scores_picks_assigned_unscored_items(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending.db'}")
    await db.prepare()
    wanted, _ = await _seed(db, title="已归组未打分", url="https://a.example/1")
    await _seed(db, title="已归组已打分", url="https://b.example/2", scored=True)
    await _seed(db, title="未归组", url="https://c.example/3", assigned=False)

    rows = await ev._pending_scores(db, EditorialSection(), NOW, limit=10)
    assert [row.news_item_id for row in rows] == [wanted], "只打「已归组且未打分」的报道"
    await db.dispose()


async def test_pending_scores_skips_non_editorial_items(tmp_path: Any) -> None:
    """社区讨论不参与精选：给它们打分没有意义（不进日报）。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending2.db'}")
    await db.prepare()
    await _seed(db, title="社区讨论", url="https://news.ycombinator.com/1", editorial=False)
    assert await ev._pending_scores(db, EditorialSection(), NOW, limit=10) == []
    await db.dispose()


async def test_pending_scores_skips_locked_stories(tmp_path: Any) -> None:
    """人工锁定的事件不自动打分——否则人工干预会被自动流程覆盖。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending3.db'}")
    await db.prepare()
    await _seed(db, title="已锁定", url="https://a.example/1", locked=True)
    assert await ev._pending_scores(db, EditorialSection(), NOW, limit=10) == []
    await db.dispose()


async def test_pending_scores_respects_the_limit(tmp_path: Any) -> None:
    """逐条两次调用 = 双倍成本，上限必须生效。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending4.db'}")
    await db.prepare()
    for index in range(5):
        await _seed(db, title=f"第 {index} 条", url=f"https://a.example/{index}")
    rows = await ev._pending_scores(db, EditorialSection(), NOW, limit=2)
    assert len(rows) == 2
    await db.dispose()


async def test_pending_scores_uses_the_stored_tier(tmp_path: Any) -> None:
    """信源分级用已落库的值（归组时定的），不要再猜一遍。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending5.db'}")
    await db.prepare()
    await _seed(db, title="官方发布", url="https://openai.com/news/1", tier="T1")
    rows = await ev._pending_scores(db, EditorialSection(official_domains=["openai.com"]), NOW, limit=10)
    assert rows[0].tier == "T1"
    await db.dispose()


# ── 重建 ──


async def test_reset_clears_the_event_layer_but_keeps_news(tmp_path: Any) -> None:
    """重建只清事件层：资讯库是采集成果，清掉就得重新抓一遍。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}")
    await db.prepare()
    await _seed(db, title="某公司发布芯片", url="https://a.example/1", scored=True)
    counts = await ev.reset(db)
    assert counts == {"hot_snapshot": 0, "report_item": 1, "story_score": 1, "story": 1}
    assert await _count(db, Story) == 0
    assert await _count(db, ReportItem) == 0
    assert await _count(db, StoryScore) == 0
    assert await _count(db, NewsItem) == 1, "资讯库必须原样保留"
    await db.dispose()


async def test_reset_is_idempotent(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'reset2.db'}")
    await db.prepare()
    await _seed(db, title="x", url="https://a.example/1")
    await ev.reset(db)
    assert (await ev.reset(db))["story"] == 0
    await db.dispose()


# ── 刷新 ──


async def test_refresh_on_empty_db_does_nothing(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh.db'}")
    await db.prepare()
    llm = FakeLLM()
    stats = await ev.refresh(db, llm, now=NOW)
    assert stats.loaded == 0
    assert stats.stories == 0
    assert llm.calls == [], "没有候选时不该有任何模型调用"
    await db.dispose()


async def test_refresh_clusters_then_scores(tmp_path: Any) -> None:
    """一次刷新把三步都跑到：聚簇 → 打分 → 快照。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh2.db'}")
    await db.prepare()
    await _seed(db, title="某公司发布新一代推理芯片", url="https://a.example/1", assigned=False)
    # 打分的两次调用各回一个分数（只有一条未归组，聚簇不需要调用模型）
    llm = FakeLLM(replies=[reply("9"), reply("8")])
    stats = await ev.refresh(db, llm, now=NOW)
    assert stats.loaded == 1
    assert stats.stories == 1, "未归组的资讯要新建事件"
    assert stats.scored == 1
    assert stats.selected == 1
    assert await _count(db, StoryScore) == 1
    # 只有一家来源的事件**不**进榜（进榜要求参与者 ≥2 且编辑类来源 ≥1），
    # 所以这里不该有快照——快照只写给够门槛的事件
    assert await _count(db, HotSnapshot) == 0, "独家报道不是热点，不该留快照"
    await db.dispose()


async def test_refresh_does_not_rescore(tmp_path: Any) -> None:
    """打分是两次模型调用；内容没变就不该重打。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh3.db'}")
    await db.prepare()
    await _seed(db, title="某公司发布新一代推理芯片", url="https://a.example/1", assigned=False)
    await ev.refresh(db, FakeLLM(replies=[reply("9"), reply("9")]), now=NOW)
    llm = FakeLLM()
    stats = await ev.refresh(db, llm, now=NOW)
    assert stats.loaded == 0, "已经归组过了，没有新候选"
    assert stats.scored == 0
    assert llm.calls == []
    await db.dispose()


async def test_refresh_can_skip_steps(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh4.db'}")
    await db.prepare()
    await _seed(db, title="某公司发布新一代推理芯片", url="https://a.example/1", assigned=False)
    stats = await ev.refresh(db, FakeLLM(), editorial=EditorialSection(), now=NOW, do_select=False, do_snapshot=False)
    assert stats.stories == 1
    assert stats.scored == 0
    assert await _count(db, StoryScore) == 0
    await db.dispose()


async def test_refresh_survives_a_cluster_failure(tmp_path: Any) -> None:
    """聚簇失败不该让整次刷新失败：打分与快照仍然要跑。"""

    class Boom(FakeLLM):
        async def complete(self, messages: Any, tools: Any = None, *, max_tokens: Any = None) -> Any:
            raise RuntimeError("模型挂了")

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh5.db'}")
    await db.prepare()
    await _seed(db, title="Qwen-Image 2.1 Turbo 发布", url="https://a.example/1", assigned=False)
    await _seed(db, title="阿里发布 qwen-image-2.1-turbo", url="https://b.example/2", assigned=False)
    stats = await ev.refresh(db, Boom(), now=NOW)
    assert stats.stories == 2, "模型不可用时各自成事件，内容不丢"
    await db.dispose()


def test_stats_line_mentions_every_step() -> None:
    stats = ev.RefreshStats(
        loaded=10, clustered=10, stories=8, assigned=10, scored=6, selected=2, unstable=1, snapshots=3
    )
    text = stats.line()
    assert "候选 10" in text
    assert "新建事件 8 个" in text
    assert "入选 2" in text
    assert "快照 3" in text
