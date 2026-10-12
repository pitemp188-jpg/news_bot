"""
模块: core.db
职责: 数据库引擎与会话——SQLite（WAL）、异步 session、建表与轻量结构升级
依赖: core.models
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from newsbot.core.log import get_logger
from newsbot.core.models import Base

logger = get_logger(__name__)


def _enable_wal(dbapi_connection: object, _record: object) -> None:
    """开启 WAL 并设置忙等待，避免后台任务与投递并发写时直接判锁冲突。"""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


_SQLITE = sqlite.dialect()


def _column_ddl(column: Any) -> str:
    """渲染 ADD COLUMN 子句。

    SQLite 对 ADD COLUMN 有两条限制，这里必须绕开：不能加 UNIQUE / PRIMARY KEY，也不能
    加没有默认值的 NOT NULL。新列一律是可空或带常量默认值的标量列（日期、计数、短文本），
    所以照类型渲染即可；真要加约束或改类型，走 `migrations/` 的 alembic 迁移。
    """
    ddl = f'"{column.name}" {column.type.compile(dialect=_SQLITE)}'
    if column.nullable:
        return ddl
    value = getattr(column.default, "arg", None)
    if isinstance(value, bool):
        return f"{ddl} NOT NULL DEFAULT {int(value)}"
    if isinstance(value, int | float):
        return f"{ddl} NOT NULL DEFAULT {value}"
    if isinstance(value, str):
        return f"{ddl} NOT NULL DEFAULT '{value.replace(chr(39), chr(39) * 2)}'"
    # 没有常量默认值（如 default=_now 这类可调用对象）：SQLite 会拒绝 NOT NULL，
    # 退成可空列并记日志——读取端本来就要能处理 NULL，能起来比严格更重要
    logger.warning("字段 %s 是 NOT NULL 但没有常量默认值，按可空列补上", column.name)
    return ddl


class Database:
    """数据库门面：创建引擎、建表、轻量结构升级、开事务。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _enable_wal)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def prepare(self) -> None:
        """确保表结构能满足当前代码：补建缺失的表，并给已有表补上缺失的列。

        为什么需要补列：`create_all` 只建**缺失的表**，对已存在的表一个列都不动。所以给
        ORM 模型新增字段后，老库（本地那个跑了两天的库、你服务器上那个）不会自动获得新列，
        服务一启动就在查询时报 `no such column`。实测本地库正是这样：表是 create_all 建的、
        alembic 记账为空。

        为什么不用 alembic：脚手架（`migrations/`）在，但从未接线——没有 migrate 子命令、
        启动也不跑迁移。而当前的结构变更需求就是"加表、加可空列"，SQLite 的
        `ALTER TABLE ADD COLUMN` 完全够用，且幂等、无需人工确认基线。破坏性变更（删列、
        改类型、加约束）仍须走 alembic，分工写在 docs/01-architecture.md 第 6 节。
        """
        await self.init()
        added = await self._add_missing_columns()
        if added:
            logger.info("数据库结构已升级，补上 %d 个缺失字段: %s", len(added), "、".join(added))

    async def _add_missing_columns(self) -> list[str]:
        """按 ORM 定义补齐已有表缺的列，返回补了哪些（`表.列`）。"""
        added: list[str] = []
        async with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = await self._columns_of(conn, table.name)
                if not existing:
                    continue  # 表不存在的话 create_all 已经建好了
                for column in table.columns:
                    if column.name in existing:
                        continue
                    statement = text(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(column)}')
                    await conn.execute(statement)
                    added.append(f"{table.name}.{column.name}")
        return added

    @staticmethod
    async def _columns_of(conn: Any, table: str) -> set[str]:
        rows = (await conn.execute(text(f'PRAGMA table_info("{table}")'))).fetchall()
        return {row[1] for row in rows}

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
