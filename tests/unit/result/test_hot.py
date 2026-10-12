"""
模块: tests.unit.result.test_hot
职责: 校验热度算法——窗口、半衰期、独立参与者去重、门槛、趋势与标记
依赖: result.hot
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import NewsItem, ReportItem, Story
from newsbot.result import hot

AT = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _signal(participant: str, hours_before: float, *, editorial: bool = True) -> hot.Signal:
    return hot.Signal(participant=participant, observed_at=AT - timedelta(hours=hours_before), editorial=editorial)


# ── 衰减与基础计数 ──


def test_decay_halves_every_half_life() -> None:
    assert hot.decay(0) == 1.0
    assert hot.decay(hot.HALF_LIFE_HOURS) == 0.5
    assert hot.decay(hot.HALF_LIFE_HOURS * 2) == 0.25


def test_single_signal_counts_once() -> None:
    heat = hot.heat_at([_signal("qbitai", 0)], AT)
    assert heat.heat == 1.0
    assert heat.participants == 1
    assert heat.editorial_participants == 1
    assert heat.latest_at == AT


def test_same_participant_publishing_ten_times_still_counts_once() -> None:
    """一家媒体发十篇也只算一次：否则榜单会被单个媒体的刷屏占满。"""
    signals = [_signal("qbitai", hours) for hours in range(0, 10)]
    heat = hot.heat_at(signals, AT)
    assert heat.participants == 1
    assert heat.heat == 1.0, "取最新那次，不是把十次加起来"


def test_repeated_collection_of_same_time_does_not_add() -> None:
    """同一时刻重复抓到的同一条不去重就会多算——用同一时间戳两次模拟。"""
    signals = [_signal("qbitai", 1), _signal("qbitai", 1)]
    assert hot.heat_at(signals, AT).heat == hot.decay(1)


def test_older_signals_decay_more() -> None:
    fresh = hot.heat_at([_signal("a", 1)], AT)
    stale = hot.heat_at([_signal("a", 40)], AT)
    assert fresh.heat > stale.heat
    assert stale.heat > 0, "窗口内不该归零"


# ── 窗口边界 ──


def test_signal_outside_window_is_ignored() -> None:
    assert hot.heat_at([_signal("a", hot.WINDOW_HOURS + 1)], AT).participants == 0


def test_signal_exactly_at_window_edge_is_ignored() -> None:
    """窗口是左开右闭：正好 48 小时前的证据不算，避免边界上的重复计数。"""
    assert hot.heat_at([_signal("a", hot.WINDOW_HOURS)], AT).participants == 0


def test_future_signal_is_ignored() -> None:
    future = hot.Signal(participant="a", observed_at=AT + timedelta(hours=1))
    assert hot.heat_at([future], AT).participants == 0


def test_heat_is_monotonic_as_time_moves_forward() -> None:
    """时间往前走，既有证据只会衰减：同一条报道在 1 小时后热度必须更低。"""
    signals = [_signal("a", 0), _signal("b", 0)]
    now = hot.heat_at(signals, AT)
    later = hot.heat_at(signals, AT + timedelta(hours=3))
    assert later.heat < now.heat


# ── 门槛 ──


def test_single_participant_does_not_qualify() -> None:
    """一条独家报道不是热点：至少两个独立参与者才进榜。"""
    assert hot.heat_at([_signal("a", 1)], AT).qualifies is False


def test_two_participants_qualify() -> None:
    assert hot.heat_at([_signal("a", 1), _signal("b", 2)], AT).qualifies is True


def test_only_signal_sources_do_not_qualify() -> None:
    """全是社区讨论、没有一个编辑类来源时不进榜：无法核对内容。"""
    heat = hot.heat_at([_signal("x", 1, editorial=False), _signal("y", 2, editorial=False)], AT)
    assert heat.participants == 2
    assert heat.editorial_participants == 0
    assert heat.qualifies is False


def test_mixed_sources_qualify_and_count_both() -> None:
    heat = hot.heat_at([_signal("a", 1), _signal("x", 2, editorial=False)], AT)
    assert heat.participants == 2
    assert heat.editorial_participants == 1
    assert heat.qualifies is True


# ── 趋势 ──


def test_trend_new_when_before_was_empty() -> None:
    now = hot.heat_at([_signal("a", 1), _signal("b", 1)], AT)
    earlier = hot.heat_at([_signal("a", 1), _signal("b", 1)], AT - timedelta(hours=hot.TREND_GAP_HOURS))
    assert hot.trend(now, earlier) == ("new", None)


def test_trend_up_then_flat() -> None:
    """6 小时前只有 1 个参与者、现在 3 个 → up；两边证据相同 → flat。"""
    signals = [_signal("a", 7), _signal("b", 1), _signal("c", 1)]
    now = hot.heat_at(signals, AT)
    before = hot.heat_at(signals, AT - timedelta(hours=hot.TREND_GAP_HOURS))
    grade, pct = hot.trend(now, before)
    assert grade == "up"
    assert pct is not None and pct > 0.1

    same = hot.heat_at(signals, AT)
    assert hot.trend(same, hot.heat_at(signals, AT))[0] == "flat"


def test_trend_down() -> None:
    signals = [_signal("a", 40)]
    now = hot.heat_at(signals, AT)
    before = hot.heat_at(signals, AT - timedelta(hours=hot.TREND_GAP_HOURS))
    assert hot.trend(now, before)[0] == "down"


def test_trend_threshold_ignores_small_moves() -> None:
    """10% 以内的涨跌不标出来：读者看不出差别，标了反而像有事发生。"""
    before = hot.Heat(heat=1.0, participants=1, editorial_participants=1, latest_at=AT, first_at=AT)
    slight = hot.Heat(heat=1.05, participants=1, editorial_participants=1, latest_at=AT, first_at=AT)
    assert hot.trend(slight, before)[0] == "flat"


# ── 标记 ──


def test_badge_new_by_first_seen() -> None:
    heat = hot.heat_at([_signal("a", 1), _signal("b", 2)], AT)
    assert "new" in hot.badges(heat, AT, None)


def test_badge_new_disappears_after_six_hours() -> None:
    heat = hot.heat_at([_signal("a", 8), _signal("b", 9)], AT)
    assert "new" not in hot.badges(heat, AT, None)


def test_badge_surge_needs_count_and_ratio() -> None:
    """6 小时内 ≥3 个新参与者且占比过半 → surge。"""
    fresh = hot.heat_at([_signal(f"p{i}", 1) for i in range(3)], AT)
    assert "surge" in hot.badges(fresh, AT, None)

    # 只有 2 个新的、且有 3 个旧的 → 不满足阈值
    mixed = hot.heat_at([_signal("a", 1), _signal("b", 1), _signal("c", 30), _signal("d", 30)], AT)
    assert "surge" not in hot.badges(mixed, AT, None)


def test_badge_surge_and_rising_are_not_both_added() -> None:
    heat = hot.heat_at([_signal(f"p{i}", 1) for i in range(4)], AT)
    marks = hot.badges(heat, AT, pct=0.9)
    assert "surge" in marks
    assert "rising" not in marks, "已有 surge 就不再叠 rising，标记要能一眼看完"


def test_badge_rising_on_sharp_growth() -> None:
    heat = hot.heat_at([_signal("a", 30), _signal("b", 1)], AT)
    assert "rising" in hot.badges(heat, AT, pct=0.5)


def test_index_uses_readable_scale() -> None:
    heat = hot.heat_at([_signal("a", 12), _signal("b", 12)], AT)
    assert heat.index == round(heat.heat * 100) / 10
    assert 0 < heat.index < 20


# ── 落库与榜单 ──


async def _seed(db: Database, story_title: str, participants: list[tuple[str, float, bool]]) -> int:
    """造一条事件与其报道，返回事件 id。"""
    async with db.session() as session:
        story = Story(title=story_title, first_seen_at=AT - timedelta(hours=2))
        session.add(story)
        await session.flush()
        for index, (name, hours, editorial) in enumerate(participants):
            item = NewsItem(url=f"https://a/{story.id}-{index}", url_hash=f"h{story.id}-{index}", title=name)
            session.add(item)
            await session.flush()
            session.add(
                ReportItem(
                    story_id=story.id,
                    news_item_id=item.id,
                    participant=name,
                    observed_at=AT - timedelta(hours=hours),
                    editorial=editorial,
                )
            )
        await session.commit()
        return story.id


async def test_ranking_sorts_by_heat_and_filters_weak_stories(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'hot.db'}")
    await db.prepare()
    try:
        await _seed(db, "冷门独家", [("a", 1, True)])  # 只有 1 个参与者，不该进榜
        await _seed(db, "两家中等", [("a", 5, True), ("b", 5, True)])
        await _seed(db, "四家在说", [("a", 1, True), ("b", 1, True), ("c", 1, True), ("d", 1, True)])

        rows = await hot.ranking(db, AT)
        assert [row.title for row in rows] == ["四家在说", "两家中等"]
        assert rows[0].participants == 4
        assert rows[0].sources == 4
    finally:
        await db.dispose()


async def test_ranking_reads_community_signals_as_participants(tmp_path: Path) -> None:
    """社区讨论算参与者（热度），但不进"来源数"（无法核对内容）。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'hot2.db'}")
    await db.prepare()
    try:
        await _seed(db, "含社区讨论", [("a", 1, True), ("hackernews", 1, False), ("dev", 1, False)])
        rows = await hot.ranking(db, AT)
        assert rows[0].participants == 3
        assert rows[0].sources == 1
    finally:
        await db.dispose()


async def test_ranking_is_empty_for_fresh_db(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    await db.prepare()
    try:
        assert await hot.ranking(db, AT) == []
    finally:
        await db.dispose()


async def test_snapshot_is_written_once_per_hour_and_updated(tmp_path: Path) -> None:
    """同一小时内重复算要覆盖而不是插两条：证据在变，旧值作废。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'snap.db'}")
    await db.prepare()
    try:
        story_id = await _seed(db, "事件", [("a", 1, True), ("b", 1, True)])
        assert await hot.snapshot(db, AT) == 1
        later = AT + timedelta(minutes=10)  # 同一小时内再算一次
        assert await hot.snapshot(db, later) == 1

        from newsbot.core.models import HotSnapshot

        async with db.session() as session:
            rows = (await session.execute(select(HotSnapshot).where(HotSnapshot.story_id == story_id))).scalars().all()
        assert len(rows) == 1, "同一小时只能有一条"
        assert rows[0].participants == 2
    finally:
        await db.dispose()


async def test_snapshot_skips_weak_stories(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'snap2.db'}")
    await db.prepare()
    try:
        await _seed(db, "独家", [("a", 1, True)])
        assert await hot.snapshot(db, AT) == 0
    finally:
        await db.dispose()
