"""
模块: api.serializers
职责: 把 ORM 行转成前端需要的 JSON 结构（时间统一 ISO 字符串）
依赖: core.models
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


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
