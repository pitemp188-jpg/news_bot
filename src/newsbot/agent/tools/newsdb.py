"""
模块: agent.tools.newsdb
职责: 本地资讯库查询工具——按关键词与天数复用已采集内容，减少重复联网
依赖: agent.tools.base, core.db, core.models
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import select

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.db import Database
from newsbot.core.models import NewsItem

MAX_ROWS = 10
SNIPPET_CHARS = 200


class NewsDbTool:
    """查询已入库资讯，命中时优先复用，避免重复抓取。"""

    name = "news_db"
    description = "查询本地已采集的资讯库。存在近期同类内容时优先使用，可节省联网与抓取成本。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "标题关键词，留空表示不限"},
            "days": {"type": "integer", "description": "只看最近多少天，默认 7"},
        },
        "required": [],
    }

    def __init__(self, db: Database, *, now: Any = None) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(UTC))

    async def run(self, keyword: str = "", days: int = 7, **_: Any) -> ToolResult:
        since = self._now() - timedelta(days=max(1, int(days or 7)))
        try:
            async with self._db.session() as session:
                statement = select(NewsItem).where(NewsItem.fetched_at >= since)
                if keyword:
                    statement = statement.where(NewsItem.title.contains(str(keyword)))
                statement = statement.order_by(NewsItem.fetched_at.desc()).limit(MAX_ROWS)
                rows = (await session.execute(statement)).scalars().all()
        except Exception as exc:  # 数据库暂不可用时不要让整个任务失败
            return ToolResult.failure(f"资讯库查询失败: {exc}")

        if not rows:
            return ToolResult(text="资讯库中没有匹配的记录。")
        lines = []
        for index, item in enumerate(rows, 1):
            snippet = (item.title or "").strip()[:SNIPPET_CHARS]
            lines.append(f"{index}. {snippet}\n   {item.url}")
        sources = [Source(title=item.title or item.url, url=item.url) for item in rows]
        return ToolResult(text="\n".join(lines), sources=sources)

    async def aclose(self) -> None:
        return None
