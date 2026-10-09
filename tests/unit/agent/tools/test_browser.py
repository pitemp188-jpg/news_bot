"""
模块: tests.unit.agent.test_browser
职责: 校验浏览器工具的降级提示、替身注入、并发限制与错误处理
依赖: agent.tools.browser
"""

from __future__ import annotations

import asyncio

from tests.fakes.search import FakeBrowserRunner

from newsbot.agent.tools.browser import INSTALL_HINT, BrowserTool
from newsbot.core.config import AgentSection


def _settings(**overrides: object) -> AgentSection:
    return AgentSection(**overrides)  # type: ignore[arg-type]


async def test_injected_runner_returns_text_and_source() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(text="页面要点"))
    result = await tool.run("https://news.example/a", "提取要点")
    assert "页面要点" in result.text
    assert result.sources[0].url == "https://news.example/a"
    await tool.aclose()


async def test_missing_url_is_reported() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner())
    assert "缺少网址" in (await tool.run(" ")).text
    await tool.aclose()


async def test_empty_runner_output_reports_failure() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(text="   "))
    result = await tool.run("https://news.example/a")
    assert "未提取到内容" in result.text
    await tool.aclose()


async def test_runner_error_is_contained() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(error=RuntimeError("浏览器崩溃")))
    result = await tool.run("https://news.example/a")
    assert "浏览器执行出错" in result.text
    await tool.aclose()


async def test_missing_dependency_reports_hint() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(error=ImportError("no browser_use")))
    result = await tool.run("https://news.example/a")
    assert INSTALL_HINT in result.text
    await tool.aclose()


async def test_long_output_is_truncated() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(text="长" * 9000))
    result = await tool.run("https://news.example/a")
    assert "内容过长已截断" in result.text
    await tool.aclose()


async def test_concurrency_is_limited() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(browser_concurrency=1), runner=runner)
    await asyncio.gather(*(tool.run(f"https://news.example/{i}", "提取") for i in range(4)))
    assert runner.peak == 1
    await tool.aclose()
