"""
模块: tests.live.test_live_crawl
职责: 真实抓取——公网页面下载与正文抽取，验证非本地站点的通用性
依赖: agent.tools.fetch

运行前置：可访问公网。用 `uv run python -m pytest -m live tests/live` 运行。
"""

from __future__ import annotations

import pytest

from newsbot.agent.tools.fetch import MAX_TEXT_CHARS, FetchTool
from newsbot.core.config import AgentSection

pytestmark = pytest.mark.live

# 结构极简、长期稳定，适合做可重复的抓取断言
STABLE_URL = "https://example.com/"
STABLE_TITLE = "Example Domain"


async def test_live_fetch_public_page() -> None:
    """真实下载公网页面并抽出正文，标题与正文都要有内容。"""
    tool = FetchTool(AgentSection(), allow_private=False)
    try:
        result = await tool.run(STABLE_URL)
    finally:
        await tool.aclose()

    assert "失败" not in result.text, result.text
    # 正文来自 <p>，标题来自 <title>，两者都要抽到
    assert "documentation examples" in result.text
    assert result.sources and result.sources[0].url == STABLE_URL
    assert result.sources[0].title == STABLE_TITLE


async def test_live_fetch_rejects_ssrf_and_bad_input() -> None:
    """真实路径下 SSRF 防护与空参数校验同样生效。"""
    tool = FetchTool(AgentSection(), allow_private=False)
    try:
        assert "缺少网址" in (await tool.run("")).text
        blocked = await tool.run("http://169.254.169.254/latest/meta-data/")
        assert "不允许访问" in blocked.text
        assert (await tool.run("http://127.0.0.1:1/")).text.startswith("[工具执行失败]")
    finally:
        await tool.aclose()


async def test_live_fetch_truncates_long_page() -> None:
    """长页面的正文被截断到可读长度，避免撑爆模型上下文。"""
    tool = FetchTool(AgentSection(), allow_private=False, timeout=30.0)
    try:
        result = await tool.run("https://www.iana.org/help/example-domains")
    finally:
        await tool.aclose()

    assert "失败" not in result.text, result.text
    assert len(result.text) <= MAX_TEXT_CHARS + 32
