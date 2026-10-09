"""
模块: tests.unit.agent.test_search
职责: 校验 SearXNG / Tavily 调用、结果归一化与错误分类
依赖: agent.tools.search
"""

from __future__ import annotations

import httpx
import pytest
import respx

from newsbot.agent.tools.search import SearchTool
from newsbot.core.config import AgentSection, Secrets
from newsbot.core.errors import RetriableError

SEARX = "http://127.0.0.1:8080/search"
TAVILY = "https://api.tavily.com/search"


def _tool(provider: str = "searxng", **overrides: object) -> SearchTool:
    secrets = Secrets(search_provider=provider, tavily_api_key="tvly-test")
    settings = AgentSection(search_results=int(overrides.get("search_results", 5)))  # type: ignore[arg-type]
    return SearchTool(settings, secrets)


@respx.mock
async def test_searxng_normalizes_results() -> None:
    respx.get(SEARX).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "标题一", "url": "https://a.example/1", "content": "摘要一"},
                    {"title": "", "url": "https://a.example/2"},
                ]
            },
        )
    )
    tool = _tool()
    result = await tool.run("AI 新闻", time_range="day")
    assert "标题一" in result.text
    assert "无标题" in result.text
    assert [source.url for source in result.sources] == ["https://a.example/1", "https://a.example/2"]
    assert respx.calls[0].request.url.params["time_range"] == "day"
    await tool.aclose()


@respx.mock
async def test_searxng_limits_result_count() -> None:
    payload = {"results": [{"title": f"t{i}", "url": f"https://a.example/{i}", "content": "s"} for i in range(10)]}
    respx.get(SEARX).mock(return_value=httpx.Response(200, json=payload))
    tool = _tool(search_results=3)
    result = await tool.run("AI")
    assert len(result.sources) == 3
    await tool.aclose()


@respx.mock
async def test_tavily_is_used_when_configured() -> None:
    respx.post(TAVILY).mock(
        return_value=httpx.Response(200, json={"results": [{"title": "T", "url": "https://b.example", "content": "c"}]})
    )
    tool = _tool("tavily")
    result = await tool.run("AI")
    assert result.sources[0].url == "https://b.example"
    assert respx.calls[0].request.headers["content-type"] == "application/json"
    await tool.aclose()


async def test_empty_query_is_reported() -> None:
    tool = _tool()
    assert "缺少搜索关键词" in (await tool.run("   ")).text
    await tool.aclose()


@respx.mock
async def test_no_results_returns_plain_text() -> None:
    respx.get(SEARX).mock(return_value=httpx.Response(200, json={"results": []}))
    tool = _tool()
    result = await tool.run("冷门词")
    assert result.text == "没有搜索到相关结果。"
    assert result.sources == []
    await tool.aclose()


@respx.mock
async def test_server_error_is_retriable() -> None:
    respx.get(SEARX).mock(return_value=httpx.Response(500))
    tool = _tool()
    with pytest.raises(RetriableError):
        await tool.run("AI")
    await tool.aclose()


@respx.mock
async def test_timeout_is_retriable() -> None:
    respx.get(SEARX).mock(side_effect=httpx.ConnectTimeout("慢"))
    tool = _tool()
    with pytest.raises(RetriableError):
        await tool.run("AI")
    await tool.aclose()


@respx.mock
async def test_missing_tavily_key_returns_empty() -> None:
    tool = SearchTool(AgentSection(), Secrets(search_provider="tavily", tavily_api_key=""))
    result = await tool.run("AI")
    assert result.text == "没有搜索到相关结果。"
    await tool.aclose()
