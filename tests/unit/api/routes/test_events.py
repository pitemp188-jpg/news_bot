"""
模块: tests.unit.api.test_events
职责: 校验事件层接口——事件列表、事件详情、热度榜与日报编排
依赖: newsbot.api.routes.events
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from newsbot.core.models import NewsItem, ReportItem, Story, StoryScore

NOW = datetime.now(UTC)


async def _seed(api: Any, *, title: str, reports: list[tuple[str, str, str, bool]], hours: float = 1) -> int:
    """建一个事件与它的报道；`reports` 每项是 (participant, tier, role, editorial)。"""
    async with api.app.db.session() as session:
        story = Story(
            title=title,
            first_seen_at=NOW - timedelta(hours=hours),
            last_seen_at=NOW - timedelta(hours=hours),
        )
        session.add(story)
        await session.flush()
        for index, (participant, tier, role, editorial) in enumerate(reports):
            url = f"https://{participant}.example/{story.id}-{index}"
            item = NewsItem(url=url, url_hash=url, title=f"{title} - {participant}", source=participant)
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
                    observed_at=NOW - timedelta(hours=hours - index / 10),
                )
            )
            session.add(StoryScore(news_item_id=item.id, score1=8, score2=8, selected=True))
        await session.commit()
        return story.id


async def test_events_requires_login(api: Any) -> None:
    assert (await api.get("/api/events")).status_code == 401


async def test_events_lists_recent_stories(api: Any) -> None:
    await api.login()
    story_id = await _seed(
        api, title="某公司发布芯片", reports=[("qbitai", "T2", "primary", True), ("infoq", "T2", "duplicate", True)]
    )
    response = await api.get("/api/events")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["id"] == story_id
    assert payload["items"][0]["participants"] == 2, "参与者按去重后的人/机构算，不是按报道条数"


async def test_events_counts_a_media_publishing_ten_posts_once(api: Any) -> None:
    """一家媒体发十篇也只算一个参与者——页面口径必须与热度算法一致。"""
    await api.login()
    await _seed(api, title="刷屏事件", reports=[("qbitai", "T2", "primary", True)] * 10)
    payload = (await api.get("/api/events")).json()
    assert payload["items"][0]["participants"] == 1


async def test_events_excludes_old_stories(api: Any) -> None:
    await api.login()
    await _seed(api, title="很久以前", reports=[("qbitai", "T2", "primary", True)], hours=24 * 30)
    payload = (await api.get("/api/events?days=7")).json()
    assert payload["items"] == []


async def test_event_detail_returns_reports_with_roles(api: Any) -> None:
    await api.login()
    story_id = await _seed(
        api,
        title="某公司发布芯片",
        reports=[
            ("qbitai", "T2", "primary", True),
            ("infoq", "T2", "follow_up", True),
            ("other", "T1", "duplicate", True),
        ],
    )
    response = await api.get(f"/api/events/{story_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["story"]["id"] == story_id
    roles = {row["role"] for row in payload["reports"]}
    assert roles == {"primary", "follow_up", "duplicate"}
    assert payload["reports"][0]["score"] == 8.0


async def test_event_detail_404_for_unknown_id(api: Any) -> None:
    await api.login()
    assert (await api.get("/api/events/99999")).status_code == 404


async def test_hot_requires_login(api: Any) -> None:
    assert (await api.get("/api/hot")).status_code == 401


async def test_hot_exposes_the_rule(tmp_path: Any, api: Any) -> None:
    """页面要能说清榜单是按什么算的——规则版本与窗口写进响应。"""
    await api.login()
    payload = (await api.get("/api/hot")).json()
    assert payload["rule"]
    assert payload["window_hours"] == 48
    assert payload["half_life_hours"] == 24


async def test_hot_lists_qualifying_stories(api: Any) -> None:
    await api.login()
    story_id = await _seed(
        api, title="某公司发布芯片", reports=[("qbitai", "T2", "primary", True), ("infoq", "T2", "duplicate", True)]
    )
    payload = (await api.get("/api/hot")).json()
    assert [row["story_id"] for row in payload["items"]] == [story_id]


async def test_hot_skips_stories_without_an_editorial_source(api: Any) -> None:
    """只有社区讨论、没有编辑类来源的事件不进榜：热度高但不代表事情重要。"""
    await api.login()
    await _seed(
        api,
        title="社区自嗨",
        reports=[("news.ycombinator.com", "T2", "primary", False), ("lobste.rs", "T2", "duplicate", False)],
    )
    assert (await api.get("/api/hot")).json()["items"] == []


async def test_digest_returns_one_entry_per_story(api: Any) -> None:
    await api.login()
    await _seed(
        api, title="某公司发布芯片", reports=[("qbitai", "T2", "primary", True), ("infoq", "T2", "duplicate", True)]
    )
    payload = (await api.get("/api/digest?period=daily")).json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["participants"] == 2
    assert payload["period"] == "daily"


async def test_digest_rejects_unknown_period(api: Any) -> None:
    await api.login()
    assert (await api.get("/api/digest?period=hourly")).status_code == 422


async def test_digest_on_empty_db(api: Any) -> None:
    await api.login()
    payload = (await api.get("/api/digest")).json()
    assert payload["items"] == []


async def test_events_counts_participants_across_stories(api: Any) -> None:
    """两个事件各自的参与者数不能互相串。"""
    await api.login()
    first = await _seed(
        api, title="甲事件", reports=[("a.example", "T2", "primary", True), ("b.example", "T2", "duplicate", True)]
    )
    second = await _seed(api, title="乙事件", reports=[("c.example", "T2", "primary", True)])
    async with api.app.db.session() as session:
        rows = (await session.scalars(select(Story))).all()
    assert {row.id for row in rows} == {first, second}
    payload = (await api.get("/api/events")).json()
    counts = {row["title"]: row["participants"] for row in payload["items"]}
    assert counts == {"甲事件": 2, "乙事件": 1}
