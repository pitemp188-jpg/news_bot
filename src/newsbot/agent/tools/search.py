"""
模块: agent.tools.search
职责: 搜索工具——SearXNG 与 Tavily 统一为标题 / 链接 / 摘要
依赖: agent.tools.base, core.config, core.errors, core.log
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection, Secrets
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger

logger = get_logger(__name__)

TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30}


class SearchTool:
    """按配置选择搜索引擎，统一返回可读文本与来源。"""

    name = "search"
    description = "搜索互联网，返回标题、链接和摘要。了解最新信息时先用它，再按需抓取正文。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month"],
                "description": "限定时间范围，查最新资讯时使用",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        settings: AgentSection,
        secrets: Secrets,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def provider(self) -> str:
        return self._secrets.search_provider.lower()

    async def run(self, query: str, time_range: str | None = None, **_: Any) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult.failure("缺少搜索关键词")
        try:
            if self.provider == "tavily":
                hits = await self._tavily(query)
            else:
                hits = await self._searxng(query, time_range)
        except httpx.TimeoutException as exc:
            raise RetriableError(f"搜索超时: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RetriableError(f"搜索返回 {exc.response.status_code}") from exc

        if not hits:
            return ToolResult(text="没有搜索到相关结果。")
        lines = [f"{index}. {hit['title']}\n   {hit['url']}\n   {hit['snippet']}" for index, hit in enumerate(hits, 1)]
        sources = [Source(title=hit["title"], url=hit["url"]) for hit in hits]
        return ToolResult(text="\n".join(lines), sources=sources)

    async def _searxng(self, query: str, time_range: str | None) -> list[dict[str, str]]:
        params: dict[str, Any] = {"q": query, "format": "json"}
        if time_range in TIME_RANGE_DAYS:
            params["time_range"] = time_range
        response = await self._http().get(f"{self._secrets.searxng_url.rstrip('/')}/search", params=params)
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": str(item.get("title") or "无标题"),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "").strip(),
            }
            for item in (data.get("results") or [])[: self._settings.search_results]
        ]

    async def _tavily(self, query: str) -> list[dict[str, str]]:
        if not self._secrets.tavily_api_key:
            return []
        response = await self._http().post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._secrets.tavily_api_key,
                "query": query,
                "max_results": self._settings.search_results,
                "search_depth": "basic",
            },
        )
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": str(item.get("title") or "无标题"),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "").strip(),
            }
            for item in (data.get("results") or [])[: self._settings.search_results]
        ]

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
