"""
模块: tests.live.test_live_browser
职责: 真实浏览器采集——用 browser-use 打开真实站点，核对抽取结果与页面原文一致、无编造字段
依赖: agent.tools.browser

运行前置：已安装可选依赖（uv sync --extra browser）且可访问公网。
用 `uv run python -m pytest -m live tests/live` 运行。

为什么单独一个文件：browser-use 的真实代价与失败模式（模型速度、扩展下载、浏览器启动）
无法用替身覆盖，而信息质量必须拿真实页面核对——把子 Agent 报的数字逐个回查页面原文。
"""

from __future__ import annotations

import re

import httpx
import pytest

from newsbot.agent.tools.browser import BrowserTool
from newsbot.core.config import AgentSection, Secrets

pytestmark = pytest.mark.live

TRENDING_URL = "https://github.com/trending"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
# 页面上真实存在的字段：trending 只公布「今日新增 star」，没有总 star / fork
NUMBER_RE = re.compile(r"\d[\d,]{2,}")


def _require_browser() -> None:
    from newsbot.agent.tools.browser import browser_use_available

    if not browser_use_available():
        pytest.skip("未安装 browser-use（uv sync --extra browser），跳过真实浏览器用例")


def _tool(tmp_path_factory: pytest.TempPathFactory) -> BrowserTool:
    return BrowserTool(
        AgentSection(browser_max_steps=8),
        secrets=Secrets(),
        data_dir=tmp_path_factory.mktemp("browser-data"),
    )


async def test_live_browser_reads_real_dynamic_page(tmp_path_factory: pytest.TempPathFactory) -> None:
    """真实动态页面必须能读出内容，且来源就是访问的网址。"""
    _require_browser()
    tool = _tool(tmp_path_factory)
    try:
        result = await tool.run(
            TRENDING_URL,
            "提取当前排在前 3 位的仓库，每个给出仓库全名、今日新增 star 数、一句话中文简介",
        )
    finally:
        await tool.aclose()

    assert not result.text.startswith("[工具执行失败]"), result.text
    assert result.sources and result.sources[0].url == TRENDING_URL
    # 前 3 名的仓库名要真的出现
    assert len(re.findall(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result.text)) >= 3


async def test_live_browser_numbers_are_grounded_in_page(tmp_path_factory: pytest.TempPathFactory) -> None:
    """子 Agent 报出的每个数字都必须能在页面原文里找到——这是防编造的关键断言。

    实测背景：放宽约束时它会补充「总 star」「fork」等 trending 页面根本没有的字段，
    并给出看似合理的数字；收紧契约后这些字段消失，剩下的数字全部可回查。
    """
    _require_browser()
    tool = _tool(tmp_path_factory)
    try:
        result = await tool.run(
            TRENDING_URL,
            "提取当前排在前 3 位的仓库，每个给出仓库全名、今日新增 star 数、一句话中文简介",
        )
    finally:
        await tool.aclose()

    html = httpx.get(TRENDING_URL, headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True).text
    numbers = NUMBER_RE.findall(result.text)
    assert numbers, f"没有抽取到任何数字，输出为: {result.text}"
    ungrounded = [value for value in numbers if value not in html]
    assert not ungrounded, f"以下数字在页面上找不到，疑似编造: {ungrounded}"


async def test_live_browser_skips_unrequested_fields(tmp_path_factory: pytest.TempPathFactory) -> None:
    """未被要求的字段不应出现在输出里（曾实测到补充不存在的 star 总数）。"""
    _require_browser()
    tool = _tool(tmp_path_factory)
    try:
        result = await tool.run(TRENDING_URL, "提取当前排在前 3 位的仓库全名与今日新增 star 数")
    finally:
        await tool.aclose()

    for pattern in (r"总\s*star", r"star\s*总数", r"\bfork", r"语言[：:]"):
        assert not re.search(pattern, result.text, re.I), f"出现未要求的字段 {pattern}: {result.text}"


async def test_live_browser_reports_missing_page_gracefully(tmp_path_factory: pytest.TempPathFactory) -> None:
    """页面确实没有目标信息时要说没有，而不是编一个答案出来。"""
    _require_browser()
    tool = _tool(tmp_path_factory)
    question = "提取该页面公布的「2026 年季度财报营收数字」；如果页面没有这类信息就直接说明没有"
    try:
        result = await tool.run("https://example.com/", question)
    finally:
        await tool.aclose()

    # 必须明确说没有，而不是给出一个答案
    assert "页面未提供" in result.text or "没有" in result.text, result.text
    # 输出里不允许出现提问之外的新数字（提问自带的年份会被原样引用，不算编造）
    invented = set(NUMBER_RE.findall(result.text)) - set(NUMBER_RE.findall(question))
    assert not invented, f"编造了提问之外的数据: {invented}，输出: {result.text}"
