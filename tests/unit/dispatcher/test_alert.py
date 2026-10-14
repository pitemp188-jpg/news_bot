"""
模块: tests.unit.dispatcher.test_alert
职责: 校验新事件告警——谁算"新事件"、谁够格、节流与首轮静默、失败要能重试
依赖: dispatcher.alert
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from newsbot.core.db import Database
from newsbot.core.models import Delivery, NewsItem, ReportItem, Story, StoryScore
from newsbot.dispatcher import alert

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class FakeSender:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        self.sent.append((platform, chat_id, text))
        return self.ok


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'alert.db'}")
    await db.prepare()
    return db


async def _story(
    db: Database,
    *,
    title: str,
    sources: int = 2,
    hours: float = 1,
    selected: bool = True,
    alerted: datetime | None = None,
) -> int:
    """建一个事件，带 `sources` 家独立来源的报道（都带评分）。"""
    moment = NOW - timedelta(hours=hours)
    async with db.session() as session:
        story = Story(title=title, first_seen_at=moment, last_seen_at=moment, alerted_at=alerted)
        session.add(story)
        await session.flush()
        for index in range(sources):
            item = NewsItem(
                url=f"https://{title}-{index}.example/a",
                url_hash=f"{title}-{index}",
                title=f"{title}（第 {index + 1} 家）",
                source=f"src{index + 1}.com",
                summary="摘要",
                published_at=moment,
                fetched_at=moment,
            )
            session.add(item)
            await session.flush()
            session.add(
                ReportItem(
                    story_id=story.id,
                    news_item_id=item.id,
                    participant=f"src{index + 1}.com",
                    tier="T2",
                    role="primary" if index == 0 else "duplicate",
                    editorial=True,
                    observed_at=moment,
                )
            )
            session.add(StoryScore(news_item_id=item.id, score1=9, score2=9, selected=selected))
        await session.commit()
        return story.id


async def _alerted_at(db: Database, story_id: int) -> datetime | None:
    async with db.session() as session:
        return (await session.get(Story, story_id)).alerted_at  # type: ignore[union-attr]


# ── 判定 ──


async def test_notify_pushes_a_fresh_selected_event(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        story_id = await _story(db, title="两家都报了这件事")
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=False)
        assert stats.candidates == 1
        assert stats.sent == 1
        assert len(sender.sent) == 1
        platform, chat_id, text = sender.sent[0]
        assert (platform, chat_id) == ("qqbot", "c1")
        assert "新事件" in text
        assert "两家都报了这件事" in text
        assert "2 家报道" in text, "指标要与榜单同口径"
        assert await _alerted_at(db, story_id) is not None, "推成功要记下来，否则每轮都重推"
    finally:
        await db.dispose()


async def test_notify_skips_events_that_already_went_out(tmp_path: Path) -> None:
    """推过一次就不再打扰——这是"告警"与"定时推整张榜"的区别所在。"""
    db = await _make_db(tmp_path)
    try:
        await _story(db, title="已经推过的事", alerted=NOW - timedelta(hours=1))
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=False)
        assert stats.sent == 0
        assert sender.sent == []
        assert "都已经推送过" in stats.line()
    finally:
        await db.dispose()


async def test_notify_skips_events_without_selection(tmp_path: Path) -> None:
    """光"多少家在说"不等于"值得看"：没过入选门槛的不推（那是日报的同一把尺子）。"""
    db = await _make_db(tmp_path)
    try:
        await _story(db, title="不够格的事", selected=False)
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=False)
        assert stats.candidates == 1
        assert stats.sent == 0
        assert stats.skipped_unselected == 1
        assert sender.sent == []
    finally:
        await db.dispose()


async def test_notify_ignores_events_that_are_not_new(tmp_path: Path) -> None:
    """6 小时前就有热度的事件不算"新出现"——它已经在榜上了，再推一次是噪音。"""
    db = await _make_db(tmp_path)
    try:
        # 12 小时前就有报道：6 小时前的窗口里热度不是 0，趋势不会是 new
        await _story(db, title="早就出现过的事", hours=12)
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=False)
        assert stats.candidates == 0
        assert sender.sent == []
    finally:
        await db.dispose()


async def test_notify_ignores_single_source_events(tmp_path: Path) -> None:
    """独家报道进不了榜（要 ≥2 家独立来源），也就不该告警。"""
    db = await _make_db(tmp_path)
    try:
        await _story(db, title="只有一家报的事", sources=1)
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=False)
        assert stats.candidates == 0
    finally:
        await db.dispose()


# ── 护栏 ──


async def test_notify_caps_a_burst(tmp_path: Path) -> None:
    """爆发时宁可漏推也不刷屏：一次最多推 max_per_run 条，按热度排序取前面的。"""
    db = await _make_db(tmp_path)
    try:
        for index in range(5):
            await _story(db, title=f"第 {index} 件事", hours=1 + index * 0.1)
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, max_per_run=2, bootstrap=False)
        assert stats.candidates == 5
        assert stats.sent == 2
        assert len(sender.sent) == 1, "一批只发一条消息，不是一条一个事件"
    finally:
        await db.dispose()


async def test_notify_bootstraps_on_first_ever_run(tmp_path: Path) -> None:
    """上线首轮只标记不推送。

    没有这道护栏，第一次打开开关就会把库里积压的历史事件一次推光，之后再也不敢开。
    """
    db = await _make_db(tmp_path)
    try:
        ids = [await _story(db, title=f"历史事件 {index}") for index in range(3)]
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=True)
        assert stats.sent == 0
        assert stats.bootstrapped == 3
        assert sender.sent == [], "首轮一条都不许发"
        for story_id in ids:
            assert await _alerted_at(db, story_id) is not None, "要标记，否则下一轮还会被当首轮"
    finally:
        await db.dispose()


async def test_notify_is_not_bootstrapping_once_something_was_alerted(tmp_path: Path) -> None:
    """已经推过东西之后再出现的真新事件要正常推——静默只该发生在最初那一轮。"""
    db = await _make_db(tmp_path)
    try:
        await _story(db, title="早就推过的", alerted=NOW - timedelta(hours=2))
        await _story(db, title="刚出现的新事")
        sender = FakeSender()
        stats = await alert.notify(db, sender, chat_id="c1", now=NOW, bootstrap=True)
        assert stats.bootstrapped == 0
        assert stats.sent == 1
        assert "刚出现的新事" in sender.sent[0][2]
    finally:
        await db.dispose()


async def test_notify_keeps_going_when_send_fails(tmp_path: Path) -> None:
    """发送失败**不**标记已推，下一轮还会重试；否则一条失败就永久静默。"""
    db = await _make_db(tmp_path)
    try:
        story_id = await _story(db, title="发不出去的事")
        stats = await alert.notify(db, FakeSender(ok=False), chat_id="c1", now=NOW, bootstrap=False)
        assert stats.failed == 1
        assert stats.sent == 0
        assert await _alerted_at(db, story_id) is None, "失败不能标记，否则再也不会重试"

        retry = FakeSender()
        assert (await alert.notify(db, retry, chat_id="c1", now=NOW, bootstrap=False)).sent == 1
    finally:
        await db.dispose()


async def test_notify_without_sender_or_target(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        await _story(db, title="两家都报了这件事")
        no_channel = await alert.notify(db, None, now=NOW, bootstrap=False)
        assert no_channel.sent == 0 and "通道未就绪" in no_channel.line()
        # 没有配置目标、也没有任何成功投递记录 → 不推，并说清楚原因
        no_target = await alert.notify(db, FakeSender(), now=NOW, bootstrap=False)
        assert no_target.sent == 0 and "没有可用的告警目标" in no_target.line()
    finally:
        await db.dispose()


async def test_notify_on_quiet_db(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        stats = await alert.notify(db, FakeSender(), now=NOW, bootstrap=False)
        assert stats.sent == 0
        assert stats.line() == "没有新出现的事件"
    finally:
        await db.dispose()


# ── 目标解析 ──


async def test_resolve_target_prefers_the_configured_chat(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        assert await alert.resolve_target(db, "qqbot", "configured") == ("qqbot", "configured")
    finally:
        await db.dispose()


async def test_resolve_target_falls_back_to_the_last_sent_delivery(tmp_path: Path) -> None:
    """没配置目标时回退到最近一次成功投递过的会话。

    平台有主动推送限制，只有用户先给机器人发过消息才有可用目标——"最近成功投递过"
    恰好保证了这个条件。
    """
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add(Delivery(platform="qqbot", chat_id="old", status="sent"))
            session.add(Delivery(platform="qqbot", chat_id="failed", status="failed"))
            await session.commit()
        assert await alert.resolve_target(db, "qqbot", "") == ("qqbot", "old")
    finally:
        await db.dispose()


async def test_resolve_target_returns_none_when_nothing_ever_sent(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        assert await alert.resolve_target(db, "qqbot", "") is None
    finally:
        await db.dispose()


def test_alert_line_does_not_repeat_the_caller_prefix() -> None:
    """统计行不带「新事件」前缀——调用方已经写了「新事件告警：」。

    不加这句断言时的实际日志是「新事件告警：新事件 3 个 → 推送 1 条」，同一个词连着
    出现两次；日志是本项目唯一能在长跑里回答「做过什么」的地方，读起来不该有噪音。
    """
    line = alert.AlertStats(candidates=3, sent=1, skipped_unselected=2).line()
    assert line.startswith("候选 3 个 → 推送 1 条"), line
    assert "新事件" not in line

    quiet = alert.AlertStats()
    assert quiet.line() == "没有新事件"
