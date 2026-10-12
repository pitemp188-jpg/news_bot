"""
模块: agent.tools.newsdb
职责: 本地资讯库查询工具——按关键词与天数复用已采集内容，减少重复联网
依赖: agent.tools.base, core.db, core.models
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import func, select

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.db import Database
from newsbot.core.models import NewsItem

MAX_ROWS = 10
SNIPPET_CHARS = 200


class NewsDbTool:
    """查询已入库资讯，命中时优先复用，避免重复抓取。"""

    name = "news_db"
    description = "查询本地已采集的资讯库（返回标题、链接与**发布时间**）。存在近期同类内容时优先使用。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "标题关键词，留空表示不限"},
            "days": {
                "type": "integer",
                "description": "只看最近多少天发布的内容，默认 7。按条目的发布时间算，不是我们抓取的时间",
            },
        },
        "required": [],
    }

    def __init__(self, db: Database, *, now: Any = None) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(UTC))

    async def run(self, keyword: str = "", days: int = 7, **_: Any) -> ToolResult:
        since = self._now() - timedelta(days=max(1, int(days or 7)))
        # 按**发布时间**过滤与排序，而不是抓取时间：一篇几天前的旧稿今天被重新抓到
        # 时，`fetched_at` 是今天，用它当判据会让旧闻混进"最近一天"。published_at
        # 为空（历史数据或源没给日期）时退回 fetched_at。
        stamp = func.coalesce(NewsItem.published_at, NewsItem.fetched_at)
        try:
            async with self._db.session() as session:
                statement = select(NewsItem).where(stamp >= since)
                if keyword:
                    statement = statement.where(NewsItem.title.contains(str(keyword)))
                statement = statement.order_by(stamp.desc()).limit(MAX_ROWS)
                rows = (await session.execute(statement)).scalars().all()
        except Exception as exc:  # 数据库暂不可用时不要让整个任务失败
            return ToolResult.failure(f"资讯库查询失败: {exc}")

        if not rows:
            return ToolResult(text="资讯库中没有匹配的记录。")
        lines = []
        for index, item in enumerate(rows, 1):
            title = (item.title or "").strip()[:SNIPPET_CHARS]
            origin = (item.source or "").strip() or _host(item.url)
            # 显示必须用**发布时间**，不能拿抓取时间顶替：一篇半年前的旧稿今天被重新
            # 抓到时会显示成今天，"最新进展"就成了谎话（这正是本次要修的缺陷）。
            # 发布时间未知就如实标注，并附上抓取时间供判断存货够不够新。过滤那一步
            # 允许退回抓取时间（见上），是为了历史数据不被整批丢弃——判据与展示的
            # 职责不同，不能因为过滤宽松就把展示也放宽。
            if item.published_at is not None:
                when = _short_time(item.published_at)
            else:
                when = f"日期未知，抓取于 {_short_time(item.fetched_at)}"
            lines.append(f"{index}. {title}（{origin} · {when}）\n   {item.url}")
        sources = [
            Source(
                title=item.title or item.url,
                url=item.url,
                published=_iso(item.published_at),
                source=(item.source or "").strip(),
            )
            for item in rows
        ]
        return ToolResult(text="\n".join(lines), sources=sources)

    async def aclose(self) -> None:
        return None


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1).removeprefix("www.") if match else (url or "")


def _iso(value: Any) -> str:
    """datetime → ISO 字符串；没有就返回空，让下游知道这条没有发布时间。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    return ""


def _short_time(value: Any) -> str:
    """压成「MM-DD HH:MM」给模型看；没有时间就标注日期未知。"""
    moment = value if isinstance(value, datetime) else None
    if moment is None:
        return "日期未知"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{moment.astimezone():%m-%d %H:%M}"
