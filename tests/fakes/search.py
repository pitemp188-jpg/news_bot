"""
模块: tests.fakes.search
职责: 搜索与浏览器替身——返回预设命中，避免联网
依赖: agent.tools.base
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newsbot.agent.tools.base import Source, ToolResult


@dataclass
class FakeSearchTool:
    """返回预设命中的搜索替身。"""

    hits: list[dict[str, str]] = field(default_factory=list)
    name: str = "search"
    description: str = "测试用搜索"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}
    )
    queries: list[str] = field(default_factory=list)

    async def run(self, query: str = "", **_: Any) -> ToolResult:
        self.queries.append(query)
        if not self.hits:
            return ToolResult(text="没有搜索到相关结果。")
        lines = [
            f"{i}. {hit['title']}\n   {hit['url']}\n   {hit.get('snippet', '')}" for i, hit in enumerate(self.hits, 1)
        ]
        return ToolResult(text="\n".join(lines), sources=[Source(title=h["title"], url=h["url"]) for h in self.hits])

    async def aclose(self) -> None:
        return None


@dataclass
class FakeBrowserRunner:
    """浏览器子 Agent 替身：返回固定文本或抛错，并记录并发峰值。"""

    text: str = "浏览器提取到的正文"
    error: Exception | None = None
    active: int = 0
    peak: int = 0
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, url: str, task: str) -> str:
        import asyncio

        self.calls.append((url, task))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.05)
            if self.error is not None:
                raise self.error
            return self.text
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        return None
