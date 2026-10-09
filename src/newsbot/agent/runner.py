"""
模块: agent.runner
职责: 规划循环——按预算驱动工具调用，汇总带来源的采集结果
依赖: agent.prompts, agent.tools.base, core.config, core.llm, core.log
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from newsbot.agent.prompts import build_messages
from newsbot.agent.tools.base import Tool, ToolResult, tool_schema
from newsbot.core.config import AgentSection
from newsbot.core.llm import LLM, ToolCall
from newsbot.core.log import get_logger

logger = get_logger(__name__)


@dataclass
class Findings:
    """一次采集的结果：结论文本、来源列表与预算消耗。"""

    answer: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    budget_exhausted: bool = False


def _assistant_message(content: str, calls: list[ToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in calls
        ],
    }


class Runner:
    """工具调用循环；步数、token 双重预算，超限即收敛并说明情况。"""

    def __init__(self, llm: LLM, tools: list[Tool], settings: AgentSection) -> None:
        self._llm = llm
        self._tools = {tool.name: tool for tool in tools}
        self._settings = settings

    async def run(self, query: str, history: list[dict[str, Any]] | None = None) -> Findings:
        messages = build_messages(query, history)
        schemas = [tool_schema(tool) for tool in self._tools.values()]
        sources: dict[str, dict[str, str]] = {}
        tokens = 0

        for step in range(1, self._settings.max_steps + 1):
            reply = await self._llm.complete(messages, tools=schemas or None)
            tokens += reply.prompt_tokens + reply.completion_tokens
            if not reply.tool_calls:
                return Findings(
                    answer=reply.content.strip(),
                    sources=list(sources.values()),
                    steps=step,
                    tokens=tokens,
                )

            messages.append(_assistant_message(reply.content, reply.tool_calls))
            for call in reply.tool_calls:
                result = await self._invoke(call)
                for source in result.sources:
                    sources.setdefault(source.url, source.as_dict())
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result.text})

            if tokens >= self._settings.max_tokens:
                logger.warning("任务 token 超出预算 %d，提前收敛", self._settings.max_tokens)
                return self._summarize(sources, tokens, step, "token 预算")

        return self._summarize(sources, tokens, self._settings.max_steps, "步数上限")

    async def _invoke(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            available = "、".join(self._tools)
            return ToolResult.failure(f"未知工具 {call.name}；可用工具: {available}")
        try:
            return await tool.run(**call.arguments)
        except Exception as exc:  # 工具异常交给模型自行调整策略
            logger.warning("工具 %s 执行失败: %s", call.name, exc)
            return ToolResult.failure(f"{call.name}: {exc}")

    def _summarize(self, sources: dict[str, dict[str, str]], tokens: int, steps: int, reason: str) -> Findings:
        if sources:
            listed = "\n".join(f"- {item['title']} {item['url']}" for item in sources.values())
            answer = f"已达到{reason}，未能给出完整结论。已收集到以下来源：\n{listed}"
        else:
            answer = f"已达到{reason}，未能收集到可用信息，请稍后重试或换一种问法。"
        return Findings(
            answer=answer, sources=list(sources.values()), steps=steps, tokens=tokens, budget_exhausted=True
        )

    async def aclose(self) -> None:
        for tool in self._tools.values():
            await tool.aclose()
