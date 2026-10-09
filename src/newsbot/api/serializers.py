"""
模块: api.serializers
职责: 把 ORM 行转成前端需要的 JSON 结构（时间统一 ISO 字符串）
依赖: core.models
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task


def _iso(value: datetime | None) -> str | None:
    """转 ISO 字符串并强制带时区。

    SQLite 读回来的 datetime 是不带 tzinfo 的，直接 isoformat 会让前端把 UTC
    当成当地时间解析（实测会显示成 8 小时前）。这里对无时区的值按 UTC 补齐。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def task_dict(row: Task) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "query": row.query,
        "status": row.status,
        "platform": row.platform,
        "chat_id": row.chat_id,
        "attempts": row.attempts,
        "error": row.error,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
    }


def schedule_dict(row: Schedule) -> dict[str, Any]:
    return {
        "id": row.id,
        "cron": row.cron,
        "topics": list(row.topics or []),
        "platform": row.platform,
        "chat_id": row.chat_id,
        "enabled": bool(row.enabled),
    }


def news_dict(row: NewsItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "url": row.url,
        "title": row.title,
        "source": row.source,
        "published_at": _iso(row.published_at),
        "fetched_at": _iso(row.fetched_at),
    }


def report_dict(row: Report) -> dict[str, Any]:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "content": row.content,
        "sources": list(row.sources or []),
        "created_at": _iso(row.created_at),
    }


def delivery_dict(row: Delivery) -> dict[str, Any]:
    return {
        "id": row.id,
        "report_id": row.report_id,
        "platform": row.platform,
        "chat_id": row.chat_id,
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
        "sent_at": _iso(row.sent_at),
    }
