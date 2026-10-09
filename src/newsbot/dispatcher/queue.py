"""
模块: dispatcher.queue
职责: 任务队列——持久化任务、并发 worker、超时重试取消、重启恢复
依赖: core.config, core.db, core.errors, core.log, core.models
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from newsbot.core.config import QueueSection
from newsbot.core.db import Database
from newsbot.core.errors import FatalError, NewsbotError, RetriableError, TaskTimeoutError
from newsbot.core.log import get_logger, set_trace
from newsbot.core.models import Task

logger = get_logger(__name__)

# 执行体：拿到会话与任务，自行落库，返回回复文本（可为空）
Executor = Callable[[AsyncSession, Task], Awaitable[str | None]]

ACTIVE_STATES = ("pending", "running")


def _now() -> datetime:
    return datetime.now(UTC)


class TaskQueue:
    """单进程任务队列：状态流转集中在队列，业务逻辑由 executor 注入。"""

    def __init__(self, db: Database, settings: QueueSection, executor: Executor) -> None:
        self._db = db
        self._settings = settings
        self._executor = executor
        self._pending: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[int, asyncio.Task[None]] = {}
        self._cancelled: set[int] = set()
        self._stopping = False

    async def start(self) -> None:
        """恢复上次未完成的任务，并启动 worker。"""
        recovered = await self._recover()
        for _ in range(max(1, self._settings.concurrency)):
            self._workers.append(asyncio.create_task(self._worker()))
        for task_id in recovered:
            self._pending.put_nowait(task_id)
        if recovered:
            logger.info("已恢复 %d 个未完成任务", len(recovered))

    async def stop(self, *, grace: float = 5.0) -> None:
        self._stopping = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        running = list(self._running.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running, timeout=grace)

    async def submit(self, *, kind: str, query: str, platform: str | None = None, chat_id: str | None = None) -> Task:
        """写入任务并立即入队，返回已持久化的任务行。"""
        async with self._db.session() as session:
            task = Task(kind=kind, query=query, status="pending", platform=platform, chat_id=chat_id)
            session.add(task)
            await session.commit()
            await session.refresh(task)
        self._pending.put_nowait(task.id)
        logger.info("任务 #%d 已入队 (%s)", task.id, kind)
        return task

    async def cancel(self, task_id: int) -> bool:
        """取消等待中或执行中的任务；返回是否生效。"""
        async with self._db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in ACTIVE_STATES:
                return False
            self._cancelled.add(task_id)
            await self._set_status(session, task_id, "cancelled")
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
        logger.info("任务 #%d 已取消", task_id)
        return True

    async def find(self, task_id: int) -> Task | None:
        async with self._db.session() as session:
            return await session.get(Task, task_id)

    def set_executor(self, executor: Executor) -> None:
        """替换执行体；业务组装后再注入时用（测试也靠它制造慢任务）。"""
        self._executor = executor

    @property
    def depth(self) -> int:
        """排队中的任务数。"""
        return self._pending.qsize()

    @property
    def running(self) -> int:
        """正在执行的任务数。"""
        return len(self._running)

    async def requeue(self, task_id: int) -> bool:
        """重跑一个已结束的任务：重置状态与计数后重新入队。"""
        async with self._db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status in ACTIVE_STATES:
                return False
            task.status = "pending"
            task.attempts = 0
            task.error = None
            task.started_at = None
            task.finished_at = None
            await session.commit()
        self._cancelled.discard(task_id)
        self._pending.put_nowait(task_id)
        logger.info("任务 #%d 已重新入队", task_id)
        return True

    async def _recover(self) -> list[int]:
        """把上次残留的 pending / running 任务重新排队。"""
        async with self._db.session() as session:
            rows = (await session.execute(select(Task).where(Task.status.in_(ACTIVE_STATES)))).scalars().all()
            for task in rows:
                task.status = "pending"
                task.started_at = None
            await session.commit()
            return [task.id for task in rows]

    async def _worker(self) -> None:
        while not self._stopping:
            task_id = await self._pending.get()
            if task_id in self._cancelled:
                self._cancelled.discard(task_id)
                continue
            self._running[task_id] = asyncio.current_task()  # type: ignore[assignment]
            try:
                await self._run(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("任务 #%d 处理异常", task_id)
            finally:
                self._running.pop(task_id, None)

    async def _run(self, task_id: int) -> None:
        set_trace(f"task-{task_id}")
        async with self._db.session() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in ACTIVE_STATES:
                return
            task.status = "running"
            task.attempts += 1
            task.started_at = _now()
            await session.commit()

        outcome, error = await self._execute(task_id)
        async with self._db.session() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.finished_at = _now()
            if outcome == "retry":
                task.status = "pending"
                task.error = error
                await session.commit()
                logger.warning("任务 #%d 第 %d 次失败，将重试: %s", task_id, task.attempts, error)
                await asyncio.sleep(max(0.0, self._settings.retry_delay_seconds))
                if not self._stopping:
                    self._pending.put_nowait(task_id)
                return
            task.status = outcome
            task.error = error
            await session.commit()
        logger.info("任务 #%d 结束: %s", task_id, outcome)

    async def _execute(self, task_id: int) -> tuple[str, str | None]:
        """执行任务并给出终态；只有 RetriableError 会转成重试。"""
        try:
            async with self._db.session() as session:
                task = await session.get(Task, task_id)
                if task is None:
                    return "failed", "任务不存在"
                await asyncio.wait_for(self._executor(session, task), timeout=self._settings.task_timeout_seconds)
            return "succeeded", None
        except asyncio.CancelledError:
            raise
        except TaskTimeoutError:
            return "timeout", f"超过 {self._settings.task_timeout_seconds}s"
        except TimeoutError:
            return "timeout", f"超过 {self._settings.task_timeout_seconds}s"
        except RetriableError as exc:
            attempts = await self._attempts(task_id)
            if attempts < self._settings.max_attempts:
                return "retry", str(exc)
            return "failed", f"重试 {attempts} 次仍失败: {exc}"
        except FatalError as exc:
            return "failed", str(exc)
        except NewsbotError as exc:
            return "failed", str(exc)
        except Exception as exc:
            logger.exception("任务 #%d 执行异常", task_id)
            return "failed", f"{type(exc).__name__}: {exc}"

    async def _attempts(self, task_id: int) -> int:
        async with self._db.session() as session:
            task = await session.get(Task, task_id)
            return task.attempts if task else 0

    async def _set_status(self, session: AsyncSession, task_id: int, status: str) -> None:
        task = await session.get(Task, task_id)
        if task is not None:
            task.status = status
            task.finished_at = _now()
            await session.commit()
