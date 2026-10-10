"""
模块: dispatcher.pipeline
职责: 执行流水线——Agent 采集 → 结果成稿 → 投递 → 写报告与会话历史
依赖: core.db, core.errors, core.log, core.models, dispatcher.session
"""

from __future__ import annotations

import re
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

_CITATION = re.compile(r"\[S(\d+)\]")


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


def _cited_indexes(sources: list[dict[str, Any]], answer: str) -> set[int]:
    """正文引用过哪些编号（返回来源列表里的下标）。

    这些条目**一律保留**：正文已经写了 `[S1]`，把 S1 剔掉就是引用悬空——读者在
    来源列表里找不到它。实测发生过：正文引用 S1/S11/S16，三者都被去重剔除，
    列表里只剩 S17/S20，读者无从核对。

    编号存在来源字典里（`label`），这里按它反查下标；正文里的编号形式与
    `result.report` 的约定一致（`[S3]`）。
    """
    cited = {int(number) for number in _CITATION.findall(answer)}
    if not cited:
        return set()
    protected: set[int] = set()
    for index, item in enumerate(sources):
        label = str(item.get("label", "") or "")
        if label.startswith("S") and label[1:].isdigit() and int(label[1:]) in cited:
            protected.add(index)
    return protected


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
        # 去重放在用量落库之前：语义分组会产生一次模型调用，它的用量也要进 llm_usage
        await self._drop_duplicates(task, findings)
        await self._record_usage(session, task, findings)
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

    async def _drop_duplicates(self, task: Task, findings: Any) -> None:
        """合并同一件事的多家报道；定时推送时再剔除近期已推送过的。

        判据是**内容**而不是来源：不同媒体报同一件事要合并，同一家媒体的两条不同
        新闻要都留下。保留哪一条由 `Deduper.keep_indexes` 按信息量与来源权重决定。

        **按历史剔除只用于定时推送**：定时推送的语义是"别把同一批新闻再推一遍"，
        而手动提问是用户当下想看，把他问的东西当成"已推送过"而不给来源是错的——
        实测同一话题连问两次，39 条来源被删到 9 条，正文引用的 5 个编号里 3 个
        直接消失。

        另外，**正文已经引用过的编号一律保留**（`protect`）：读者看到 `[S1]` 却在
        来源列表里找不到它，属于引用悬空（项目硬规则要求每个 `[Sn]` 都能在列表里
        找到）。有了这层保护就不需要"已合并"的说明——被引用的条目根本不会被剔除。
        """
        if self._deduper is None:
            return
        sources = list(getattr(findings, "sources", None) or [])
        if not sources:
            return
        deduper = await self._deduper()
        if deduper is None:
            return
        items = [
            Item(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                text=str(item.get("snippet", "") or ""),
                weight=float(item.get("weight", 1.0) or 1.0),
            )
            for item in sources
        ]
        # merge() 会先做一次可选的语义分组（模型判断"这几条是不是同一件事"），
        # 再按内容判据决定保留哪些；分组用量的写入紧跟其后。
        # 保护集＝正文已经引用过的编号；历史剔除只在定时推送时启用。
        protect = _cited_indexes(sources, str(getattr(findings, "answer", "") or ""))
        keep_history = task.kind == "scheduled"
        merge = await deduper.merge(items, protect=protect)
        for usage in deduper.usages:
            findings.usages = [*(getattr(findings, "usages", None) or []), usage]
        deduper.usages.clear()
        if not keep_history and merge.from_history:
            # `is_duplicate` 会命中历史（它不知道调用意图），手动提问时把仅因历史
            # 被剔除的条目放回来：用户当下在问，这不是"重复推送"
            merge.kept = sorted(set(merge.kept) | set(merge.from_history))
            logger.info("手动提问不做历史去重，放回 %d 条来源", len(merge.from_history))
            merge.from_history = []
        if len(merge.kept) == len(items):
            return
        logger.info("去重合并 %d 条重复内容（%d → %d）", len(items) - len(merge.kept), len(items), len(merge.kept))
        if not merge.kept:
            findings.sources = []
            findings.answer = f"{findings.answer}\n\n（以上来源彼此重复或与近期推送重复，已合并。）".strip()
            return
        findings.sources = [sources[index] for index in merge.kept]
        # 说明被剔除的编号已无必要：被正文引用过的条目在 protect 里，不会被剔除。
        # 这里只留日志，便于排查"为什么这条没列出来"。

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
                    # 入库用的内容要与去重时的判据一致（标题 + 摘要），否则历史指纹永远命中不了
                    body = f"{item.get('title', '')} {item.get('snippet', '')}".strip() or str(item.get("url", ""))
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
