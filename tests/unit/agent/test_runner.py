"""
模块: tests.unit.agent.test_runner
职责: 校验规划循环的工具调用、来源汇总、预算收敛与异常容错
依赖: agent.runner
"""

from __future__ import annotations

from typing import Any, ClassVar

from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.runner import Runner
from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection
from newsbot.core.llm import ToolCall


class BoomTool:
    """调用即抛错的工具，用于验证异常被收敛成文本。"""

    name = "boom"
    description = "测试用"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.closed = False

    async def run(self, **_: Any) -> ToolResult:
        raise RuntimeError("内部错误")

    async def aclose(self) -> None:
        self.closed = True


def _settings(**overrides: object) -> AgentSection:
    base = {"max_steps": 5, "max_tokens": 1000}
    base.update(overrides)
    return AgentSection(**base)  # type: ignore[arg-type]


async def test_tool_call_then_final_answer() -> None:
    search = FakeSearchTool(hits=[{"title": "标题", "url": "https://a.example/1", "snippet": "摘要"}])
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI"})]),
            reply("本周结论 [1]"),
        ]
    )
    runner = Runner(llm, [search], _settings())
    findings = await runner.run("今天 AI 新闻")

    assert findings.answer == "本周结论 [1]"
    assert findings.steps == 2
    assert findings.tokens == 40
    assert findings.budget_exhausted is False
    assert findings.sources == [{"title": "标题", "url": "https://a.example/1"}]
    assert search.queries == ["AI"]
    second_round = llm.calls[1]
    assert second_round[-2]["role"] == "assistant"
    assert second_round[-2]["tool_calls"][0]["id"] == "c1"
    assert second_round[-1]["role"] == "tool"
    assert "1. 标题" in second_round[-1]["content"]
    await runner.aclose()


async def test_immediate_answer_without_tools() -> None:
    llm = FakeLLM(replies=[reply("直接回答")])
    runner = Runner(llm, [], _settings())
    findings = await runner.run("你好")
    assert findings.answer == "直接回答"
    assert findings.steps == 1
    assert findings.sources == []
    await runner.aclose()


async def test_unknown_tool_is_reported_to_model() -> None:
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="missing", arguments={})]),
            reply("换个办法"),
        ]
    )
    runner = Runner(llm, [FakeSearchTool()], _settings())
    findings = await runner.run("q")
    assert findings.answer == "换个办法"
    assert "未知工具 missing" in llm.calls[1][-1]["content"]
    assert "search" in llm.calls[1][-1]["content"]
    await runner.aclose()


async def test_tool_exception_becomes_text() -> None:
    boom = BoomTool()
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="boom", arguments={})]),
            reply("知道了"),
        ]
    )
    runner = Runner(llm, [boom], _settings())
    findings = await runner.run("q")
    assert findings.answer == "知道了"
    assert "工具执行失败" in llm.calls[1][-1]["content"]
    assert "内部错误" in llm.calls[1][-1]["content"]
    await runner.aclose()
    assert boom.closed is True


async def test_step_budget_converges_with_sources() -> None:
    search = FakeSearchTool(hits=[{"title": "标题", "url": "https://a.example/1", "snippet": "s"}])
    llm = FakeLLM(
        replies=[reply("", calls=[ToolCall(id=f"c{i}", name="search", arguments={"query": "AI"})]) for i in range(3)]
    )
    runner = Runner(llm, [search], _settings(max_steps=3))
    findings = await runner.run("q")
    assert findings.budget_exhausted is True
    assert findings.steps == 3
    assert "步数上限" in findings.answer
    assert "https://a.example/1" in findings.answer
    await runner.aclose()


async def test_token_budget_stops_loop() -> None:
    search = FakeSearchTool(hits=[{"title": "标题", "url": "https://a.example/1", "snippet": "s"}])
    llm = FakeLLM(replies=[reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI"})], tokens=50)])
    runner = Runner(llm, [search], _settings(max_tokens=10))
    findings = await runner.run("q")
    assert findings.budget_exhausted is True
    assert "token 预算" in findings.answer
    await runner.aclose()


async def test_budget_without_sources_says_so() -> None:
    llm = FakeLLM(replies=[reply("", calls=[ToolCall(id="c1", name="missing", arguments={})], tokens=50)])
    runner = Runner(llm, [], _settings(max_tokens=10))
    findings = await runner.run("q")
    assert "未能收集到可用信息" in findings.answer
    await runner.aclose()


async def test_sources_are_deduplicated_by_url() -> None:
    class DupTool:
        name = "dup"
        description = "测试用"
        parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}

        async def run(self, **_: Any) -> ToolResult:
            return ToolResult(text="x", sources=[Source(title="A", url="https://same.example")])

        async def aclose(self) -> None:
            return None

    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="dup", arguments={}), ToolCall(id="c2", name="dup", arguments={})]),
            reply("好了"),
        ]
    )
    runner = Runner(llm, [DupTool()], _settings())
    findings = await runner.run("q")
    assert findings.sources == [{"title": "A", "url": "https://same.example"}]
    await runner.aclose()
