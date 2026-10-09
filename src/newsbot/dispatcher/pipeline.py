"""
模块: dispatcher.pipeline
职责: 执行流水线——Agent 采集 → 结果成稿 → 投递 → 写报告与会话历史
依赖: core.db, core.errors, core.log, core.models, dispatcher.session
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from newsbot.core.db import Database
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger
from newsbot.core.models import LlmUsage, NewsItem, Report, Task
from newsbot.dispatcher.session import SessionStore
from newsbot.result.dedup import Deduper, Item, content_digest, simhash64, simhash_hex, url_digest

logger = get_logger(__name__)


def _keep(deduper: Deduper, item: Item) -> bool:
    """重复来源返回 False；新来源登记指纹后返回 True。"""
    if deduper.is_duplicate(item):
        return False
    deduper.remember(item)
    return True


class Runner(Protocol):
    """Agent 规划循环：查询 → 带来源的采集结果。"""

    async def run(self, query: str, history: list[dict[str, Any]]) -> Any: ...


class BuiltReport(Protocol):
    """成稿结果：正文 + 来源列表。"""

    content: str
    sources: list[dict[str, str]]


class Reporter(Protocol):
    """结果处理：采集结果 → 成稿。"""

    async def build(self, task: Task, findings: Any) -> BuiltReport: ...


class Sender(Protocol):
    """投递：由 app 注入网关实现；report_id 用于关联 delivery 记录。"""

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool: ...


class DeduperFactory(Protocol):
    """按需构造去重器（已载入近期指纹）；返回 None 表示本次不去重。"""

    async def __call__(self) -> Deduper | None: ...


class Pipeline:
    """把一次任务的完整链路串起来；上下游用协议解耦，便于测试与替换。"""

    def __init__(
        self,
        db: Database,
        *,
        runner: Runner,
        reporter: Reporter,
        sender: Sender | None,
        sessions: SessionStore,
        deduper: DeduperFactory | None = None,
        remember: bool = True,
    ) -> None:
        self._db = db
        self._runner = runner
        self._reporter = reporter
        self._sender = sender
        self._sessions = sessions
        self._deduper = deduper
        self._remember = remember

    async def execute(self, session: AsyncSession, task: Task) -> str | None:
        """执行任务；投递失败按可重试处理，其余错误向上抛给队列分类。"""
        history = await self._history(task)
        findings = await self._runner.run(task.query, history)
        await self._record_usage(session, task, findings)
        await self._drop_duplicates(findings)
        built = await self._reporter.build(task, findings)
        content = built.content

        # 先提交报告拿到 id：投递是网络 IO，不能持有写事务，否则会与投递记录的写入互相等锁
        report = Report(task_id=task.id, content=content, sources=list(built.sources))
        session.add(report)
        await session.commit()

        if self._remember:
            await self._store_sources(built.sources)

        if task.platform and task.chat_id:
            await self._deliver(task, content, report.id)
        else:
            logger.info("任务 #%d 无投递目标，仅生成报告", task.id)

        if task.platform and task.chat_id:
            await self._sessions.append(task.platform, task.chat_id, user_text=task.query, reply_text=content)
        return content

    async def _history(self, task: Task) -> list[dict[str, Any]]:
        if not (task.platform and task.chat_id):
            return []
        return await self._sessions.load(task.platform, task.chat_id)

    async def _deliver(self, task: Task, content: str, report_id: int | None) -> None:
        if self._sender is None:
            raise RetriableError("投递组件未就绪")
        assert task.platform is not None and task.chat_id is not None
        sent = await self._sender.send(platform=task.platform, chat_id=task.chat_id, text=content, report_id=report_id)
        if not sent:
            raise RetriableError(f"投递失败: {task.platform}/{task.chat_id}")

    async def _record_usage(self, session: AsyncSession, task: Task, findings: Any) -> None:
        """把每次模型调用的用量写进 llm_usage，供后台成本统计。"""
        usages = list(getattr(findings, "usages", None) or [])
        if not usages:
            return
        for usage in usages:
            session.add(
                LlmUsage(
                    task_id=task.id,
                    model=str(getattr(usage, "model", "") or ""),
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                )
            )
        logger.info("任务 #%d 记录 %d 次模型调用用量", task.id, len(usages))

    async def _drop_duplicates(self, findings: Any) -> None:
        """与近期已推送内容比对，剔除重复来源；全是旧闻时直接说明。"""
        if self._deduper is None:
            return
        sources = list(getattr(findings, "sources", None) or [])
        if not sources:
            return
        deduper = await self._deduper()
        if deduper is None:
            return
        items = [Item(title=str(item.get("title", "")), url=str(item.get("url", ""))) for item in sources]
        kept = [source for source, item in zip(sources, items, strict=True) if _keep(deduper, item)]
        if len(kept) == len(items):
            return
        logger.info("去重过滤 %d 条重复来源", len(items) - len(kept))
        if not kept:
            findings.sources = []
            findings.answer = f"{findings.answer}\n\n（以上来源均与近期推送重复，已去重。）".strip()
            return
        findings.sources = kept

    async def _store_sources(self, sources: list[dict[str, str]]) -> None:
        """把新来源写入资讯库，供后续任务复用与去重。"""
        if not sources:
            return
        url_hashes = [url_digest(str(item.get("url", ""))) for item in sources]
        try:
            async with self._db.session() as session:
                existing = set(
                    (
                        await session.execute(select(NewsItem.url_hash).where(NewsItem.url_hash.in_(url_hashes)))
                    ).scalars()
                )
                for item, digest in zip(sources, url_hashes, strict=True):
                    if not digest or digest in existing:
                        continue
                    body = str(item.get("title") or item.get("url") or "")
                    session.add(
                        NewsItem(
                            url=str(item.get("url", "")),
                            url_hash=digest,
                            title=str(item.get("title", "")),
                            source=str(item.get("source", "")),
                            content_hash=content_digest(body),
                            simhash=simhash_hex(simhash64(body)),
                            fetched_at=datetime.now(UTC),
                        )
                    )
                    existing.add(digest)
                await session.commit()
        except Exception as exc:  # 入库失败不应影响本次投递
            logger.warning("资讯入库失败: %s", exc)


PipelineExecutor = Callable[[AsyncSession, Task], Awaitable[str | None]]
