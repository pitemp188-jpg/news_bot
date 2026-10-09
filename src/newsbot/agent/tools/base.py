"""
模块: agent.tools.base
职责: 工具契约与共享类型——Source / ToolResult / Tool 协议 / OpenAI 工具描述生成
依赖: 无
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Source:
    """一条可引用的来源。"""

    title: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}


@dataclass
class ToolResult:
    """工具执行结果：给模型看的文本 + 供引用的来源。"""

    text: str
    sources: list[Source] = field(default_factory=list)

    @classmethod
    def failure(cls, message: str) -> ToolResult:
        return cls(text=f"[工具执行失败] {message}")


class Tool(Protocol):
    """Agent 可调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]

    async def run(self, **kwargs: Any) -> ToolResult: ...

    async def aclose(self) -> None: ...


def tool_schema(tool: Tool) -> dict[str, Any]:
    """转成 OpenAI 兼容的工具描述。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
