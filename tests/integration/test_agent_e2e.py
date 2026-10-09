"""
模块: tests.integration.test_agent_e2e
职责: 端到端校验 Agent 链路——真实搜索替身 + 真实抓取 + 本地静态站点
依赖: agent.runner, agent.tools.fetch
"""

from __future__ import annotations

import pytest
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.prompts import SYSTEM_PROMPT
from newsbot.agent.runner import Runner
from newsbot.agent.tools.fetch import FetchTool
from newsbot.core.config import AgentSection
from newsbot.core.llm import ToolCall

pytestmark = pytest.mark.integration


async def test_search_then_fetch_local_article(local_server: str) -> None:
    url = f"{local_server}/article.html"
    search = FakeSearchTool(hits=[{"title": "示例资讯：AI 行业周报", "url": url, "snippet": "摘要"}])
    fetch = FetchTool(AgentSection(), allow_private=True)
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI 行业周报"})]),
            reply("", calls=[ToolCall(id="c2", name="fetch", arguments={"url": url})]),
            reply("模型推理成本较上代下降约三成 [1]"),
        ]
    )
    runner = Runner(llm, [search, fetch], AgentSection(max_steps=6, max_tokens=10_000))
    try:
        findings = await runner.run("最近 AI 行业有什么变化")
    finally:
        await runner.aclose()

    assert findings.answer == "模型推理成本较上代下降约三成 [1]"
    assert findings.steps == 3
    assert findings.budget_exhausted is False
    assert [source["url"] for source in findings.sources] == [url]

    tool_messages = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"]
    assert any("推理成本" in text for text in tool_messages)
    assert all("console.log" not in text for text in tool_messages)


async def test_fetch_failure_lets_model_recover(local_server: str) -> None:
    search = FakeSearchTool(hits=[{"title": "坏链接", "url": f"{local_server}/missing.html", "snippet": "摘要"}])
    fetch = FetchTool(AgentSection(), allow_private=True)
    llm = FakeLLM(
        replies=[
            reply("", calls=[ToolCall(id="c1", name="search", arguments={"query": "AI"})]),
            reply("", calls=[ToolCall(id="c2", name="fetch", arguments={"url": f"{local_server}/missing.html"})]),
            reply("页面打不开，仅根据摘要给出结论 [1]"),
        ]
    )
    runner = Runner(llm, [search, fetch], AgentSection(max_steps=6, max_tokens=10_000))
    try:
        findings = await runner.run("AI")
    finally:
        await runner.aclose()
    assert findings.answer == "页面打不开，仅根据摘要给出结论 [1]"
    tool_messages = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"]
    assert any("404" in text or "工具执行失败" in text for text in tool_messages)


async def test_system_prompt_is_sent_to_model() -> None:
    llm = FakeLLM(replies=[reply("好")])
    runner = Runner(llm, [], AgentSection())
    try:
        await runner.run("你好")
    finally:
        await runner.aclose()
    assert llm.calls[0][0] == {"role": "system", "content": SYSTEM_PROMPT}
