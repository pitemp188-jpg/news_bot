"""
模块: agent.tools.browser
职责: 浏览器工具——封装 browser-use 子 Agent 处理动态页面；复用浏览器会话、隔离运行产物
依赖: agent.tools.base, core.config, core.log
来源: 调用方式对齐 browser-use 0.11（MIT）的 Agent / BrowserSession / ChatOpenAI 接口
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar, Protocol

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection, Secrets
from newsbot.core.log import get_logger

logger = get_logger(__name__)

MAX_TASK_CHARS = 6000
INSTALL_HINT = "未安装 browser-use，无法驱动浏览器。可执行 uv sync --extra browser 安装。"
# 每步允许的动作数；太大容易一步做太多而失败，太小则步数暴涨
ACTIONS_PER_STEP = 3
# 子 Agent 单次输出的上限，browser-use 的动作是 JSON，太小会被截断成非法 JSON
MAX_COMPLETION_TOKENS = 4096
# 只依据页面真实内容作答——与主 Agent 的提示词约束保持一致
TASK_SUFFIX = (
    "输出要求：用中文作答；只报告上面明确要求的字段，不要自行补充 star 总数、fork 数、"
    "语言、发布时间等未被要求的字段；每一个数字都必须从页面原文逐字照抄，"
    "严禁凭记忆、印象或推测填写；页面没有提供的字段直接写「页面未提供」，"
    "不要用其他来源的数据补齐；不要对信息可靠性作自我评价，只陈述页面事实。"
)


def prepare_environment(data_dir: Path, timeout_seconds: int) -> None:
    """把 browser-use 的缓存、配置与浏览器 profile 收拢进 data/。

    browser-use 默认写到 ~/.cache 与 ~/.config，而项目规定运行时产物只能落在
    data/；顺带关掉匿名遥测——内网环境里这些外部请求只会拖慢启动。

    另外把浏览器启动超时对齐到我们的配置：browser-use 默认只给 30s，
    冷启动或系统繁忙时会被判超时，而失败信息里完全看不出是这个原因。
    """
    root = data_dir / "browser"
    root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("XDG_CACHE_HOME", str(root / "cache"))
    os.environ.setdefault("XDG_CONFIG_HOME", str(root / "config"))
    os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(root / "config" / "browseruse"))
    os.environ.setdefault("TIMEOUT_BrowserStartEvent", str(float(timeout_seconds)))


def browser_use_available() -> bool:
    """browser-use 是可选依赖；抽成函数便于测试替换。"""
    try:
        import browser_use  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserRunner(Protocol):
    """浏览器子 Agent 的执行接口，便于测试注入。"""

    async def run(self, url: str, task: str) -> str: ...


class _BrowserUseRunner:
    """默认实现：惰性导入 browser-use，复用同一个浏览器会话执行任务。"""

    def __init__(self, settings: AgentSection, secrets: Secrets, data_dir: Path) -> None:
        self._settings = settings
        self._secrets = secrets
        self._data_dir = data_dir
        self._session: Any = None
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._secrets.llm_model_browser or self._secrets.llm_model

    async def run(self, url: str, task: str) -> str:
        from browser_use import Agent  # 惰性导入：未安装时抛出 ImportError

        session = await self._ensure_session()
        agent: Any = Agent(
            task=f"打开 {url}，{task}。{TASK_SUFFIX}",
            llm=self._build_llm(),
            browser_session=session,
            # 我们的模型不支持视觉输入，传截图只会浪费 token 并报错
            use_vision=False,
            max_actions_per_step=ACTIONS_PER_STEP,
            llm_timeout=self._settings.browser_timeout_seconds,
            step_timeout=self._settings.browser_timeout_seconds,
        )
        history = await agent.run(max_steps=self._settings.browser_max_steps)
        return _stringify(history)

    def _build_llm(self) -> Any:
        from browser_use.llm import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            api_key=self._secrets.llm_api_key,
            base_url=self._secrets.llm_base_url,
            temperature=0.0,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )

    async def _ensure_session(self) -> Any:
        """首次调用时启动浏览器并复用；并发进入用锁保护。

        启动失败必须不留下任何状态：失败信息本身没有可操作性，但下一次调用
        应该有机会重新来过，而不是一直复用一个已经死掉的会话。
        """
        async with self._lock:
            if self._session is not None:
                return self._session
            prepare_environment(self._data_dir, self._settings.browser_timeout_seconds)
            from browser_use import BrowserProfile, BrowserSession

            profile = BrowserProfile(
                headless=self._settings.browser_headless,
                # 默认扩展需要联网下载，网络受限时会把启动拖过超时（实测 44s 未就绪）
                enable_default_extensions=False,
                # profile 落在 data/ 下，为将来复用登录态留出位置
                user_data_dir=str(self._data_dir / "browser" / "profile"),
            )
            session = BrowserSession(browser_profile=profile)
            try:
                await session.start()
            except Exception as exc:
                await _stop_quietly(session)
                # 让错误文本带上可操作的信息，原始信息只有一串事件名
                raise RuntimeError(
                    f"浏览器启动失败（可能被残留的浏览器进程占用 profile）: {type(exc).__name__}: {exc}"
                ) from exc
            self._session = session
            logger.info("浏览器会话已就绪，模型 %s，无头 %s", self.model, self._settings.browser_headless)
            return self._session

    async def aclose(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        await _stop_quietly(session)
        logger.info("浏览器会话已关闭")


async def _stop_quietly(session: Any) -> None:
    """尽力关闭会话；清理失败只记日志，不应掩盖原始错误。"""
    try:
        await session.stop()
    except Exception as exc:  # pragma: no cover - 上游关闭路径差异
        logger.warning("关闭浏览器会话失败: %s", exc)


def _stringify(history: Any) -> str:
    """从 browser-use 的历史对象里取出结论文本。"""
    if isinstance(history, str):
        return history
    for attr in ("final_result", "extracted_content"):
        value = getattr(history, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # pragma: no cover - 上游实现差异
                value = None
        if value:
            if isinstance(value, list):
                return "\n".join(str(item) for item in value if item)
            return str(value)
    return str(history)


class BrowserTool:
    """仅在搜索结果与静态抓取都拿不到内容时使用；并发与步数都受配置限制。"""

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
        secrets: Secrets | None = None,
        data_dir: Path | None = None,
        runner: BrowserRunner | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._data_dir = data_dir
        self._runner = runner
        self._semaphore = semaphore or asyncio.Semaphore(max(1, settings.browser_concurrency))

    def _resolve_runner(self) -> BrowserRunner:
        """注入优先；否则用配置构造真实运行器（缺凭证时立刻报错，不必等到连接失败）。"""
        if self._runner is not None:
            return self._runner
        if not browser_use_available():
            raise ImportError(INSTALL_HINT)
        if self._secrets is None or self._data_dir is None:
            raise RuntimeError("未注入浏览器运行器时，必须提供 secrets 与 data_dir")
        # 必须存回实例：否则 aclose() 找不到会话，浏览器进程会残留
        self._runner = _BrowserUseRunner(self._settings, self._secrets, self._data_dir)
        return self._runner

    async def run(self, url: str, task: str = "提取页面正文要点", **_: Any) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult.failure("缺少网址")
        try:
            runner = self._resolve_runner()
        except ImportError:
            return ToolResult.failure(INSTALL_HINT)
        except RuntimeError as exc:
            return ToolResult.failure(str(exc))

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
        if runner is None:
            return
        close = getattr(runner, "aclose", None)
        if callable(close):
            await close()
