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
    # 摘要/正文片段。入库时就在手里（去重要用它），存下来是给**聚簇**用的：只凭标题
    # 判断"是不是同一件事"会漏掉中英文互译的同一件事，而标题里往往只有一方措辞
    summary: Mapped[str] = mapped_column(Text, default="")
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


class Story(Base):
    """事件：多家来源报道的同一件事。

    为什么要有事件这一层：热度、日报编排、事件页都要求"同一件事只算一次"。按文章算的
    话，一家媒体发十篇就把榜单刷满了——实测的原始日报正是这样（同一消息的多份副本）。

    `first_seen_at` 与 `last_seen_at` 由归组时维护：前者用于"新事件"判定，后者用于
    排序与"最新进展"。
    """

    __tablename__ = "story"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    # 综述：由模型在归组后写，事件页与日报复用；为空表示还没写
    digest: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    # 人工改过的归属不允许被后续自动归组覆盖（AIHOT 的约定，避免"改完又被改回去"）
    locked: Mapped[bool] = mapped_column(default=False)


class ReportItem(Base):
    """报道：一篇文章对一个事件的归属。

    `role` 只有三种，它决定热度怎么算：
    - `primary`：一手报道（官方发布、首发）
    - `follow_up`：后续进展（同一件事的新发展）
    - `duplicate`：换个人再说一遍（同一事实的重复报道）

    分这三类而不是只标"是否重复"，是因为"后续进展"必须能挂到已有事件上并产生新的热度，
    而"重复报道"只贡献独立来源计数、不产生新的进展。
    """

    __tablename__ = "report_item"
    __table_args__ = (UniqueConstraint("story_id", "news_item_id", name="uq_report_story_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"), index=True)
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_item.id"), index=True)
    # 参与热度的独立主体：一家公司的多个渠道算一个，每个独立作者各算一个
    participant: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 信源分级 T1（官方一手）/ T1_5（官方账号与准官方）/ T2（媒体与个人）
    tier: Mapped[str] = mapped_column(String(8), default="T2")
    role: Mapped[str] = mapped_column(String(16), default="duplicate")
    # 「编辑内容」参与精选与日报；「热度信号」只贡献热度（如社区讨论）
    editorial: Mapped[bool] = mapped_column(default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class StoryScore(Base):
    """一条报道的模型评分。

    同一份标准独立打两次分是刻意的：`mean = (score1 + score2) / 2` 用来和门槛比较，
    `|score1 - score2|` 大说明标准在这条上不稳定，是要人工复核的信号，不是取平均就能
    掩盖的噪声。
    """

    __tablename__ = "story_score"
    __table_args__ = (UniqueConstraint("news_item_id", name="uq_score_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_item.id"), index=True)
    score1: Mapped[int] = mapped_column(default=0)
    score2: Mapped[int] = mapped_column(default=0)
    # 入选与否按「信源分级门槛」判定；落库是为了事后能解释"为什么它没进"
    selected: Mapped[bool] = mapped_column(default=False, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class HotSnapshot(Base):
    """某一小时的各事件热度，事件页走势与"6 小时前"对比都读它。

    只在**该小时被完整观测**时才落库（相关信源都已成功抓过）；否则标 `complete=False`，
    读的时候跳过——把没观测全的小时当成 0 会画出假的下跌。
    """

    __tablename__ = "hot_snapshot"
    __table_args__ = (UniqueConstraint("story_id", "hour", name="uq_hot_story_hour"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"), index=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    heat: Mapped[float] = mapped_column(default=0.0)
    participants: Mapped[int] = mapped_column(default=0)
    complete: Mapped[bool] = mapped_column(default=True)
