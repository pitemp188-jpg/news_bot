"""
模块: core.models
职责: 全部 ORM 模型（任务、订阅、会话、资讯、报告、投递、用量）
依赖: 无
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有模型的基类。"""


class Task(Base):
    """一次执行请求，含定时推送与聊天查询。"""

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # scheduled | chat | manual
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    platform: Mapped[str | None] = mapped_column(String(16), default=None)
    chat_id: Mapped[str | None] = mapped_column(String(128), default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Schedule(Base):
    """定时推送配置，调度器据此同步 job。"""

    __tablename__ = "schedule"

    id: Mapped[int] = mapped_column(primary_key=True)
    cron: Mapped[str] = mapped_column(String(32))  # 例如 0 21 * * *
    topics: Mapped[list[str]] = mapped_column(JSON, default=list)
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(default=True)


class ChatSession(Base):
    """按 (平台, 会话) 保存的最近对话，用于追问。"""

    __tablename__ = "chat_session"
    __table_args__ = (UniqueConstraint("platform", "chat_id", name="uq_session_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128), default="")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class NewsItem(Base):
    """资讯库，去重与新任务复用的事实来源。"""

    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    simhash: Mapped[str] = mapped_column(String(32), default="", index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Report(Base):
    """成稿结果，投递的输入。"""

    __tablename__ = "report"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), default=None, index=True)
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Delivery(Base):
    """投递记录；waiting_user 表示等用户下次发消息时补投。"""

    __tablename__ = "delivery"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("report.id"), default=None, index=True)
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    # 待投递文本；无报告来源（如临时通知）时补投递只依赖这里
    content: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class LlmUsage(Base):
    """每次模型调用的用量，用于成本统计与预算控制。"""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), default=None, index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
