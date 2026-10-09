"""
模块: dispatcher.pipeline
职责: 执行流水线——Agent 采集 → 结果成稿 → 投递 → 写报告与会话历史
依赖: core.db, core.errors, core.log, core.models, dispatcher.session
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from newsbot.core.db import Database
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger
from newsbot.core.models import Report, Task
from newsbot.dispatcher.session import SessionStore

logger = get_logger(__name__)


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
    ) -> None:
        self._db = db
        self._runner = runner
        self._reporter = reporter
        self._sender = sender
        self._sessions = sessions

    async def execute(self, session: AsyncSession, task: Task) -> str | None:
        """执行任务；投递失败按可重试处理，其余错误向上抛给队列分类。"""
        history = await self._history(task)
        findings = await self._runner.run(task.query, history)
        built = await self._reporter.build(task, findings)
        content = built.content

        # 先提交报告拿到 id：投递是网络 IO，不能持有写事务，否则会与投递记录的写入互相等锁
        report = Report(task_id=task.id, content=content, sources=list(built.sources))
        session.add(report)
        await session.commit()

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


PipelineExecutor = Callable[[AsyncSession, Task], Awaitable[str | None]]
