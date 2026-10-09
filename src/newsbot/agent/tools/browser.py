"""
模块: agent.tools.browser
职责: 浏览器工具——封装 browser-use 子 Agent，动态页面兜底；未安装时降级为提示
依赖: agent.tools.base, core.config, core.log
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Protocol

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection
from newsbot.core.log import get_logger

logger = get_logger(__name__)

MAX_TASK_CHARS = 6000
INSTALL_HINT = "未安装 browser-use，无法驱动浏览器。可执行 uv sync --extra browser 安装。"


class BrowserRunner(Protocol):
    """浏览器子 Agent 的执行接口，便于测试注入。"""

    async def run(self, url: str, task: str) -> str: ...


class _BrowserUseRunner:
    """默认实现：惰性导入 browser-use，跑一次浏览器任务并取回结果。"""

    def __init__(self) -> None:
        self._agent: Any = None

    async def run(self, url: str, task: str) -> str:
        from browser_use import Agent  # 惰性导入：未安装时抛出 ImportError

        from newsbot.core.config import get_config

        config = get_config()
        agent = Agent(task=f"打开 {url}，{task}", llm=None, browser=None, max_actions_per_step=3)
        history = await agent.run(max_steps=config.agent.max_steps)
        return _stringify(history)

    async def aclose(self) -> None:
        return None


def _stringify(result: Any) -> str:
    """把 browser-use 的结果转成文本，兼容字符串与结构化返回。"""
    if isinstance(result, str):
        return result
    text = getattr(result, "final_result", None)
    if callable(text):
        try:
            text = text()
        except Exception:  # pragma: no cover - 上游实现差异
            text = None
    return str(text) if text else str(result)


class BrowserTool:
    """仅在搜索结果与静态抓取都拿不到内容时使用；并发受配置限制。"""

    name = "browse"
    description = "用真实浏览器打开动态页面并执行子任务（如点击展开、翻页）。比抓取慢，仅在必要时代替 fetch 使用。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要打开的完整网址"},
            "task": {"type": "string", "description": "在页面上要完成的提取目标"},
        },
        "required": ["url"],
    }

    def __init__(
        self,
        settings: AgentSection,
        *,
        runner: BrowserRunner | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._semaphore = semaphore or asyncio.Semaphore(max(1, settings.browser_concurrency))

    async def run(self, url: str, task: str = "提取页面正文要点", **_: Any) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult.failure("缺少网址")
        try:
            runner = self._runner or _BrowserUseRunner()
        except ImportError:
            return ToolResult.failure(INSTALL_HINT)

        async with self._semaphore:
            try:
                text = await runner.run(url, task)
            except ImportError:
                return ToolResult.failure(INSTALL_HINT)
            except Exception as exc:
                logger.warning("浏览器任务失败 %s: %s", url, exc)
                return ToolResult.failure(f"浏览器执行出错: {exc}")
        if not text or not text.strip():
            return ToolResult.failure(f"浏览器未提取到内容: {url}")
        text = text.strip()
        if len(text) > MAX_TASK_CHARS:
            text = text[:MAX_TASK_CHARS] + "\n…（内容过长已截断）"
        return ToolResult(text=text, sources=[Source(title=url, url=url)])

    async def aclose(self) -> None:
        runner = self._runner
        if runner is not None:
            close = getattr(runner, "aclose", None)
            if callable(close):
                await close()
