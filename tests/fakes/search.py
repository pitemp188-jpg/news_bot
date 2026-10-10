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
    """浏览器驱动替身：按动作返回结果或抛错，并记录并发峰值。

    新接口是"下发动作"而不是"交给子 Agent 跑一个任务"，所以记录的是
    (action, params)；state 动作需要单独给文本，因为工具会在交互后回读页面。
    """

    text: str = "动作已执行。"
    state: str = "[1] 链接 标题"
    error: str = ""
    active: int = 0
    peak: int = 0
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def act(self, action: str, params: dict[str, Any]) -> Any:
        import asyncio

        from newsbot.agent.tools.browser import ActionOutcome

        self.calls.append((action, dict(params)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            # 真实驱动自己吞掉异常并翻译成可行动说明，替身照同样的契约返回
            if self.error:
                return ActionOutcome(error=self.error)
            if action == "state":
                return ActionOutcome(text=self.state)
            return ActionOutcome(text=self.text)
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        return None
