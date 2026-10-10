"""
模块: agent.runner
职责: 规划循环——按预算驱动工具调用，汇总带来源的采集结果
依赖: agent.prompts, agent.tools.base, core.config, core.llm, core.log
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from newsbot.agent.prompts import build_messages
from newsbot.agent.tools.base import Tool, ToolResult, tool_schema
from newsbot.core.config import AgentSection
from newsbot.core.llm import LLM, ToolCall, Usage
from newsbot.core.log import get_logger

logger = get_logger(__name__)

MAX_LISTED_SOURCES = 40
# 工具文本里"编号 + 换行 + 网址"的行，用全局编号替换掉（见 SourceRegistry.render）
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\.\s+(.*)$")


class SourceRegistry:
    """给每个来源分配跨工具调用稳定的编号（S1、S2…）。

    为什么必须有它：每个工具返回的列表都从 1 开始编号，模型据此写 [1]，
    但同一次任务里第 2 次搜索的 [1] 与第 1 次的 [1] 完全是两篇不同的文章。
    结果就是正文引用与附在末尾的来源列表对不上——实测日报里正文说"出自爱范儿
    早报"，而列表第 1 条却是 InfoQ 的另一篇（规范见 docs/05-testing.md 的引用要求）。
    """

    def __init__(self) -> None:
        self._by_url: dict[str, int] = {}
        self._ordered: list[dict[str, str]] = []

    def register(self, sources: list[Any]) -> None:
        for source in sources:
            url = str(source.url or "").strip()
            if not url or url in self._by_url:
                continue
            self._by_url[url] = len(self._ordered) + 1
            entry = source.as_dict()
            # 编号写进来源本身，而不是另开一个平行数组：一旦有谁过滤/切分 sources
            # （实测流水线的去重就会把 14 条删到 2 条），平行数组立刻错位，
            # 正文的 [S2] 会指到另一篇文章上。编号随来源走就不可能错位。
            entry["label"] = f"S{len(self._ordered) + 1}"
            self._ordered.append(entry)

    def label(self, url: str) -> str:
        number = self._by_url.get(str(url or "").strip())
        return f"S{number}" if number else ""

    def render(self, text: str) -> str:
        """把工具文本里的本地编号换成全局编号，让模型引用的是稳定 id。

        只改"编号行 + 紧随其后的网址行"这种结构，识别不到就原样保留，
        因此对不产生编号列表的工具（news_db、fetch）没有副作用。
        """
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = _NUMBERED_LINE.match(line)
            if match is None or index + 1 >= len(lines):
                continue
            url = lines[index + 1].strip()
            label = self.label(url)
            if label:
                lines[index] = f"{label}. {match.group(2)}"
        return "\n".join(lines)

    @property
    def entries(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._ordered]

    @property
    def labels(self) -> list[str]:
        return [str(item.get("label", "")) for item in self._ordered]


@dataclass
class Findings:
    """一次采集的结果：结论文本、来源列表与预算消耗。"""

    answer: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    budget_exhausted: bool = False
    # 每次模型调用的用量，由调用方（流水线）写入 llm_usage
    usages: list[Usage] = field(default_factory=list)

    @property
    def labels(self) -> list[str]:
        """与 sources 对齐的编号；编号本身存在每个来源里，不会错位。"""
        return [str(item.get("label", "")) for item in self.sources]


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
        registry = SourceRegistry()
        usages: list[Usage] = []
        tokens = 0

        for step in range(1, self._settings.max_steps + 1):
            reply = await self._llm.complete(messages, tools=schemas or None)
            usages.append(reply.usage())
            tokens += reply.prompt_tokens + reply.completion_tokens
            if not reply.tool_calls:
                return Findings(
                    answer=reply.content.strip(),
                    sources=registry.entries,
                    steps=step,
                    tokens=tokens,
                    usages=usages,
                )

            messages.append(_assistant_message(reply.content, reply.tool_calls))
            for call in reply.tool_calls:
                result = await self._invoke(call)
                registry.register(result.sources)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        # 换成全局编号后再给模型，引用才可能与末尾来源列表对应
                        "content": registry.render(result.text),
                    }
                )

            if tokens >= self._settings.max_tokens:
                logger.warning("任务 token 超出预算 %d，提前收敛", self._settings.max_tokens)
                return self._summarize(registry, usages, tokens, step, "token 预算")

        return self._summarize(registry, usages, tokens, self._settings.max_steps, "步数上限")

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

    def _summarize(
        self, registry: SourceRegistry, usages: list[Usage], tokens: int, steps: int, reason: str
    ) -> Findings:
        entries = registry.entries
        if entries:
            listed = "\n".join(
                f"- [{item.get('label', '')}] {item['title']} {item['url']}" for item in entries[:MAX_LISTED_SOURCES]
            )
            answer = f"已达到{reason}，未能给出完整结论。已收集到以下来源：\n{listed}"
        else:
            answer = f"已达到{reason}，未能收集到可用信息，请稍后重试或换一种问法。"
        return Findings(
            answer=answer,
            sources=entries,
            steps=steps,
            tokens=tokens,
            budget_exhausted=True,
            usages=usages,
        )

    async def aclose(self) -> None:
        for tool in self._tools.values():
            await tool.aclose()
