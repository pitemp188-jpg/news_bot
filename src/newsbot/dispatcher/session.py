"""
模块: dispatcher.session
职责: 会话上下文——按 (平台, 会话) 保存最近若干轮问答，供追问引用
依赖: core.db, core.models
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import ChatSession


class SessionStore:
    """对话历史读写；条数上限按轮数换算成消息条数。"""

    def __init__(self, db: Database, *, max_turns: int = 6) -> None:
        self._db = db
        self._limit = max(1, max_turns) * 2

    async def load(self, platform: str, chat_id: str) -> list[dict[str, Any]]:
        """返回最近的历史消息，供模型作为上下文使用。"""
        async with self._db.session() as session:
            row = await self._find(session, platform, chat_id)
            return list(row.history) if row is not None else []

    async def append(self, platform: str, chat_id: str, *, user_text: str, reply_text: str, user_id: str = "") -> None:
        """追加一轮问答并裁剪超长历史。"""
        async with self._db.session() as session:
            row = await self._find(session, platform, chat_id)
            if row is None:
                row = ChatSession(platform=platform, chat_id=chat_id, user_id=user_id, history=[])
                session.add(row)
            history = list(row.history or [])
            history.append({"role": "user", "content": user_text})
            if reply_text:
                history.append({"role": "assistant", "content": reply_text})
            row.history = history[-self._limit :]
            if user_id:
                row.user_id = user_id
            await session.commit()

    async def clear(self, platform: str, chat_id: str) -> None:
        async with self._db.session() as session:
            row = await self._find(session, platform, chat_id)
            if row is not None:
                row.history = []
                await session.commit()

    async def _find(self, session: Any, platform: str, chat_id: str) -> ChatSession | None:
        stmt = select(ChatSession).where(ChatSession.platform == platform, ChatSession.chat_id == chat_id)
        return (await session.execute(stmt)).scalar_one_or_none()
