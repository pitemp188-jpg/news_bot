"""
模块: core.db
职责: 数据库引擎与会话——SQLite（WAL）、异步 session、建表与迁移入口
依赖: core.models
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from newsbot.core.models import Base


def _enable_wal(dbapi_connection: object, _record: object) -> None:
    """开启 WAL 并设置忙等待，避免后台任务与投递并发写时直接判锁冲突。"""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """数据库门面：创建引擎、建表、开事务。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _enable_wal)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
