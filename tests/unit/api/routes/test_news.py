"""
模块: tests.unit.api.routes.test_news
职责: 校验内容接口——资讯库关键词与天数过滤、报告列表、推送记录与状态过滤
依赖: newsbot.api.routes.news
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from newsbot.core.models import Delivery, NewsItem, Report


async def test_news_filters_by_keyword_and_days(api) -> None:
    await api.login()
    await _seed(api)

    recent = (await api.get("/api/news?days=7")).json()
    assert recent["total"] == 1 and recent["items"][0]["title"] == "AI 周报"

    all_items = (await api.get("/api/news?days=365")).json()
    assert all_items["total"] == 2

    by_title = (await api.get("/api/news?days=365&keyword=芯片")).json()
    assert by_title["total"] == 1 and by_title["items"][0]["title"] == "芯片动态"

    by_url = (await api.get("/api/news?days=365&keyword=a.example")).json()
    assert by_url["total"] == 1

    none_matched = (await api.get("/api/news?days=365&keyword=不存在")).json()
    assert none_matched == {"total": 0, "items": []}

    assert (await api.get("/api/news?days=0")).status_code == 422


async def test_news_pagination(api) -> None:
    await api.login()
    await _seed(api)
    first = (await api.get("/api/news?days=365&limit=1&offset=0")).json()
    second = (await api.get("/api/news?days=365&limit=1&offset=1")).json()
    assert first["total"] == 2 and len(first["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


async def test_reports_list_newest_first(api) -> None:
    await api.login()
    await _seed(api)
    async with api.app.db.session() as session:
        session.add(Report(task_id=None, content="第二条日报"))
        await session.commit()

    body = (await api.get("/api/reports")).json()
    assert body["total"] == 2
    assert [item["content"] for item in body["items"]] == ["第二条日报", "第一条日报"]
    assert body["items"][1]["sources"] == [{"title": "t", "url": "u"}]

    assert (await api.get("/api/reports?limit=1")).json()["items"][0]["content"] == "第二条日报"


async def test_deliveries_filter_by_status(api) -> None:
    await api.login()
    async with api.app.db.session() as session:
        session.add_all(
            [
                Delivery(platform="weixin", chat_id="u1", status="sent", content="已发"),
                Delivery(platform="weixin", chat_id="u1", status="waiting_user", content="待补投"),
                Delivery(platform="qqbot", chat_id="g1", status="failed", error="平台拒绝"),
            ]
        )
        await session.commit()

    all_items = (await api.get("/api/deliveries")).json()
    assert all_items["total"] == 3
    assert {item["status"] for item in all_items["items"]} == {"sent", "waiting_user", "failed"}

    waiting = (await api.get("/api/deliveries?status=waiting_user")).json()
    assert waiting["total"] == 1
    assert waiting["items"][0]["status"] == "waiting_user"
    # 待发正文只留在库里，不通过接口下发，避免越权读取私聊内容
    assert "content" not in waiting["items"][0]

    failed = (await api.get("/api/deliveries?status=failed")).json()
    assert failed["items"][0]["error"] == "平台拒绝"

    assert (await api.get("/api/deliveries?status=unknown")).json() == {"total": 0, "items": []}


async def _seed(api) -> None:
    """两条资讯（一条 30 天前）+ 一条报告。"""
    async with api.app.db.session() as session:
        session.add_all(
            [
                NewsItem(url="https://a.example/1", url_hash="h1", title="AI 周报", source="a.example"),
                NewsItem(
                    url="https://b.example/2",
                    url_hash="h2",
                    title="芯片动态",
                    source="b.example",
                    fetched_at=datetime.now(UTC) - timedelta(days=30),
                ),
                Report(task_id=None, content="第一条日报", sources=[{"title": "t", "url": "u"}]),
            ]
        )
        await session.commit()
