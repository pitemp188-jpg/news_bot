"""
模块: tests.fakes.llm
职责: 脚本化的 LLM 替身——按预设顺序返回回复，并记录调用入参
依赖: core.llm
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newsbot.core.llm import LLMReply, ToolCall


@dataclass
class FakeLLM:
    """按脚本依次返回回复；脚本用尽后返回空回复。"""

    replies: list[LLMReply] = field(default_factory=list)
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    # 收到的输出长度上限，供"机械型任务必须限长"这类断言使用
    max_tokens_seen: list[int] = field(default_factory=list)
    closed: bool = False

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMReply:
        self.calls.append(messages)
        if max_tokens is not None:
            self.max_tokens_seen.append(max_tokens)
        return self.replies.pop(0) if self.replies else LLMReply()

    async def aclose(self) -> None:
        self.closed = True


def reply(content: str = "", *, calls: list[ToolCall] | None = None, tokens: int = 10) -> LLMReply:
    """构造一条回复，便于脚本化测试。"""
    return LLMReply(content=content, tool_calls=list(calls or []), prompt_tokens=tokens, completion_tokens=tokens)
