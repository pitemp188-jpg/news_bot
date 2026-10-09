"""
模块: tests.unit.api.test_serializers
职责: 校验 ORM 行到 JSON 的转换——字段完整性与时间格式
依赖: newsbot.api.serializers
"""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.api.serializers import delivery_dict, news_dict, report_dict, schedule_dict, task_dict
from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_task_dict_covers_all_fields() -> None:
    row = Task(id=3, kind="chat", query="问题", status="succeeded", platform="weixin", chat_id="u1", attempts=1)
    row.created_at = NOW
    row.started_at = NOW
    row.finished_at = NOW
    body = task_dict(row)
    assert body == {
        "id": 3,
        "kind": "chat",
        "query": "问题",
        "status": "succeeded",
        "platform": "weixin",
        "chat_id": "u1",
        "attempts": 1,
        "error": None,
        "created_at": NOW.isoformat(),
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
    }


def test_task_dict_handles_missing_timestamps() -> None:
    body = task_dict(Task(id=1, kind="manual", query="q", status="pending"))
    assert body["started_at"] is None and body["finished_at"] is None


def test_schedule_dict_normalises_topics_and_enabled() -> None:
    row = Schedule(id=2, cron="0 21 * * *", topics=["AI", "算力"], platform="weixin", chat_id="u1", enabled=1)
    body = schedule_dict(row)
    assert body["topics"] == ["AI", "算力"]
    assert body["enabled"] is True
    assert body["cron"] == "0 21 * * *"


def test_schedule_dict_empty_topics_is_list() -> None:
    # JSON 列可能为 None，前端按数组渲染，这里必须归一化
    row = Schedule(id=2, cron="0 21 * * *", topics=None, platform="weixin", chat_id="u1", enabled=False)
    assert schedule_dict(row)["topics"] == []


def test_news_dict_hides_internal_hashes() -> None:
    row = NewsItem(
        id=5, url="https://a.example/1", title="标题", source="a.example", url_hash="secret", content_hash="x"
    )
    row.fetched_at = NOW
    body = news_dict(row)
    assert body["url"] == "https://a.example/1"
    assert body["fetched_at"] == NOW.isoformat()
    assert "url_hash" not in body and "content_hash" not in body and "simhash" not in body


def test_report_dict_keeps_sources() -> None:
    row = Report(id=7, task_id=3, content="正文", sources=[{"title": "t", "url": "u"}])
    row.created_at = NOW
    body = report_dict(row)
    assert body["task_id"] == 3
    assert body["sources"] == [{"title": "t", "url": "u"}]


def test_delivery_dict_omits_content() -> None:
    # 待发正文可能包含私聊内容，接口不下发
    row = Delivery(id=9, report_id=7, platform="weixin", chat_id="u1", status="waiting_user", content="机密正文")
    body = delivery_dict(row)
    assert body["status"] == "waiting_user"
    assert body["report_id"] == 7
    assert "content" not in body
