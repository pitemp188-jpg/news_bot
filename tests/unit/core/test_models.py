"""
模块: tests.unit.core.test_models
职责: 校验表结构注册完整、字段默认值与取值约束
依赖: core.models
"""

from __future__ import annotations

from newsbot.core.models import Base, ChatSession, Delivery, LlmUsage, NewsItem, Report, Schedule, Task

EXPECTED_TABLES = {"task", "schedule", "chat_session", "news_item", "report", "delivery", "llm_usage"}


def test_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_task_defaults_are_callable() -> None:
    task = Task(kind="chat", query="q")
    assert task.status is None or task.status == "pending"
    assert task.attempts is None
    assert task.created_at is None


def test_session_target_is_unique() -> None:
    constraint = next(c for c in ChatSession.__table__.constraints if getattr(c, "name", "") == "uq_session_target")
    assert {column.name for column in constraint.columns} == {"platform", "chat_id"}


def test_tables_use_declared_columns() -> None:
    assert {column.name for column in Schedule.__table__.columns} >= {"cron", "topics", "enabled"}
    assert {column.name for column in Delivery.__table__.columns} >= {"status", "attempts", "sent_at"}
    assert {column.name for column in NewsItem.__table__.columns} >= {"url_hash", "simhash"}
    assert {column.name for column in Report.__table__.columns} >= {"content", "sources"}
    assert {column.name for column in LlmUsage.__table__.columns} >= {"prompt_tokens", "completion_tokens"}
