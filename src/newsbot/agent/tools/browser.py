"""
模块: agent.tools.browser
职责: 浏览器工具——把 browser-use 的动作原语直接交给 Agent，由它自己决定点哪里、输什么
依赖: agent.tools.base, core.config, core.log
来源: 动作层复用 browser-use 0.11（MIT）的 BrowserSession 与 Tools 动作注册表
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection
from newsbot.core.log import get_logger

logger = get_logger(__name__)

INSTALL_HINT = "未安装 browser-use，无法驱动浏览器。可执行 uv sync --extra browser 安装。"
# 回给 Agent 的文本上限：页面状态动辄上万字符，一次 dump 就能吃掉大半预算
MAX_OUTPUT_CHARS = 6000
# 会话启动后，等待"真正能接受指令"的上限
SESSION_READY_TIMEOUT = 30.0
# 动作名 → browser-use 动作注册表里的名字；state / tabs 是会话直调，不在表里
REGISTRY_ACTIONS = {
    "open": "navigate",
    "click": "click",
    "type": "input",
    "scroll": "scroll",
    "keys": "send_keys",
    "back": "go_back",
    "switch": "switch",
}
# 取正文用的 JS：browser-use 自带的 read_long_content 需要额外的抽取模型，
# 而我们的浏览器侧刻意不接 LLM，所以自己用 CDP evaluate 取文本
_PAGE_TEXT_JS = "() => (document.body ? document.body.innerText : '')"
_PAGE_TEXT_LIMIT = 20000
# 页面文本里空行极多，压掉后同一屏能放更多内容
_WS = re.compile(r"\n\s*\n+")
# 判定「连接已死」的线索：命中就丢掉会话，下次调用重建，而不是让 Agent 对着旧连接反复重试
_DEAD_HINTS = (
    "cdp",
    "not connected",
    "target closed",
    "browser closed",
    "connection closed",
    "session closed",
    "disconnected",
)
# 判定「被目标站点挡住」的线索：这类失败换站点比换动作更有用
_BLOCKED_HINTS = (
    "403",
    "429",
    "forbidden",
    "access denied",
    "captcha",
    "cloudflare",
    "blocked",
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


@dataclass
class ActionOutcome:
    """一条浏览器动作的结果。

    失败时必须给出**可行动**的说明（见 classify_error），而不是把底层异常原样抛出：
    主 Agent 只能看到这段文本，它靠这段文本决定重试、换页面还是换工具。
    """

    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class BrowserRunner(Protocol):
    """浏览器能力层的执行接口，便于测试注入。"""

    async def act(self, action: str, params: dict[str, Any]) -> ActionOutcome: ...

    async def aclose(self) -> None: ...


async def _wait_ready(session: Any, timeout: float) -> None:
    """等浏览器真的能接受指令再返回。

    session.start() 返回只代表进程起来了。实测紧接着下发 navigate 会报
    「CDP client not initialized - browser may not be connected yet」，
    browser-use 自带的 Agent 连试 6 次全失败后直接放弃整个任务。所以这里做一次
    真的需要 CDP 的探测（取页面状态），没就绪就等一会儿再试，直到超时。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Exception | None = None
    while True:
        try:
            await session.get_state_as_text()
            return
        except Exception as exc:
            last = exc
            if loop.time() >= deadline:
                raise RuntimeError(f"启动后 {timeout:.0f}s 内仍无法接受指令: {last}") from last
            await asyncio.sleep(0.5)


class _BrowserActions:
    """默认实现：惰性导入 browser-use，直接下发动作，不再套一层子 Agent。

    旧实现把一句自然语言任务交给 browser-use 的子 Agent，由它自己再规划一轮。
    代价是两次 LLM 开销、失败原因被吞在内部（主 Agent 只看到一句"执行出错"），
    而且子 Agent 的 prompt 与我们自己的约束是两套。改为直接下发动作后，
    浏览器不再需要任何 LLM 调用。
    """

    def __init__(self, settings: AgentSection, data_dir: Path) -> None:
        self._settings = settings
        self._data_dir = data_dir
        self._session: Any = None
        self._tools: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> tuple[Any, Any]:
        """首次调用时启动浏览器并复用；启动失败不留残余状态，下次可重来。"""
        async with self._lock:
            if self._session is not None:
                return self._session, self._tools
            prepare_environment(self._data_dir, self._settings.browser_timeout_seconds)
            from browser_use import BrowserProfile, BrowserSession
            from browser_use.tools.service import Tools

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
                await _wait_ready(session, SESSION_READY_TIMEOUT)
            except Exception as exc:
                await _stop_quietly(session)
                # 原始信息只有一串事件名，补上可操作的解释
                raise RuntimeError(
                    f"浏览器启动失败（可能被残留的浏览器进程占用 profile）: {type(exc).__name__}: {exc}"
                ) from exc
            # 先记下会话：这一行之后的任何失败（构造动作表、写日志）都必须让
            # aclose 仍能回收进程，否则就漏一个 Chromium（实测漏过一次）
            self._session = session
            # 动作注册表：navigate / click / input / scroll / send_keys / go_back / switch / read_long_content
            self._tools = Tools()
            logger.info(
                "浏览器会话已就绪，无头 %s，可用动作 %d 个",
                self._settings.browser_headless,
                _action_count(self._tools),
            )
            return session, self._tools

    async def act(self, action: str, params: dict[str, Any]) -> ActionOutcome:
        try:
            session, tools = await self._ensure()
        except ImportError:
            return ActionOutcome(error=INSTALL_HINT)
        except Exception as exc:
            return ActionOutcome(error=str(exc))
        try:
            return await asyncio.wait_for(self._dispatch(session, tools, action, params), self._timeout)
        except TimeoutError:
            return ActionOutcome(error=f"{action} 失败：超过 {self._timeout:.0f}s 未完成，页面可能一直在加载")
        except Exception as exc:
            logger.warning("浏览器动作 %s 失败: %s", action, exc)
            await self._drop_if_dead(exc)
            return ActionOutcome(error=classify_error(action, exc))

    @property
    def _timeout(self) -> float:
        return float(self._settings.browser_timeout_seconds)

    async def _dispatch(self, session: Any, tools: Any, action: str, params: dict[str, Any]) -> ActionOutcome:
        if action == "state":
            return ActionOutcome(text=_clip(await session.get_state_as_text()))
        if action == "tabs":
            return ActionOutcome(text=await _describe_tabs(session))
        if action == "text":
            return ActionOutcome(text=await _read_page_text(session))
        name = REGISTRY_ACTIONS.get(action)
        if name is None:
            return ActionOutcome(error=f"不支持的动作: {action}")
        result = await tools.registry.execute_action(name, params, browser_session=session)
        return _read_result(result)

    async def _drop_if_dead(self, exc: Exception) -> None:
        """连接类错误说明会话已经不可用，丢弃它让下次调用重建。

        不丢的话会一直复用死连接，之后每个动作都以同样的错误失败。
        """
        if not _is_dead(exc):
            return
        session, self._session = self._session, None
        self._tools = None
        if session is not None:
            await _stop_quietly(session)
        logger.info("浏览器会话已断开，已丢弃，下次调用会重建")

    async def aclose(self) -> None:
        session, self._session = self._session, None
        self._tools = None
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


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """限制回给 Agent 的文本长度；超长时明确说明被截断。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（内容过长已截断，共 {len(text)} 字符）"


def _action_count(tools: Any) -> int:
    """统计可用动作数。

    browser-use 的动作表是两层结构：`Tools.registry` 是 Registry（负责派发），
    真正的动作字典在 `Registry.registry`（ActionRegistry）里。写错这一层不会报错、
    只会在访问 `.actions` 时抛 AttributeError（实测因此让整个 open 失败）。
    """
    registry = getattr(tools, "registry", None)
    inner = getattr(registry, "registry", registry)
    return len(getattr(inner, "actions", None) or {})


def _is_dead(exc: Exception) -> bool:
    """判断异常是否意味着会话已不可用。"""
    low = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in low for hint in _DEAD_HINTS)


def classify_error(action: str, exc: Exception) -> str:
    """把底层异常翻译成 Agent 能据此决策的说明。

    原始信息只有事件名与英文栈，模型看不出该换页面、换工具还是重试——实测它只能
    猜一句「浏览工具连接异常」然后把浏览器整个放弃掉。分类后它至少知道：
    连接坏了（重试即可）、页面拒绝（换来源）、页面变了（重新看元素编号）。
    """
    raw = f"{type(exc).__name__}: {exc}"
    low = raw.lower()
    if any(hint in low for hint in _DEAD_HINTS):
        return f"{action} 失败：浏览器会话已断开（已丢弃，下次调用会自动重建，可直接重试）。{raw}"
    if any(hint in low for hint in _BLOCKED_HINTS):
        return f"{action} 失败：目标页面拒绝访问（403 或人机校验）。换一个来源，或改用 fetch 试试。{raw}"
    if "timeout" in low or "timed out" in low:
        return f"{action} 失败：等待页面超时。{raw}"
    if "index" in low and any(word in low for word in ("invalid", "not found", "out of range", "must be")):
        return f"{action} 失败：元素编号不存在，页面可能已经变化。先用 action=state 重新看一次元素编号。{raw}"
    return f"{action} 失败：{raw}"


def _read_result(result: Any) -> ActionOutcome:
    """把 browser-use 的 ActionResult 读成我们的结果。"""
    if isinstance(result, str):
        return ActionOutcome(text=_clip(result))
    error = getattr(result, "error", None)
    if error:
        return ActionOutcome(error=str(error))
    for attr in ("extracted_content", "long_term_memory"):
        value = getattr(result, attr, None)
        if value:
            return ActionOutcome(text=_clip(str(value)))
    return ActionOutcome(text="动作已执行。")


async def _read_page_text(session: Any) -> str:
    """取当前页面正文（纯文本）。

    不用 browser-use 的 read_long_content：它要求传入抽取模型，而浏览器侧刻意不接
    LLM（实测直接调用会返回「requires page_extraction_llm but none provided」）。
    这里走 CDP 执行一段 JS 取 innerText，结果按页面上限截断后回灌给 Agent。
    """
    page = await session.get_current_page()
    if page is None:
        return "当前没有可读的页面。"
    raw = await page.evaluate(_PAGE_TEXT_JS)
    text = _WS.sub("\n", str(raw or "")).strip()
    if not text:
        return "页面正文为空（可能是纯脚本页面或还没加载完）。"
    return _clip(text, _PAGE_TEXT_LIMIT)


async def _describe_tabs(session: Any) -> str:
    """列出标签页：多标签是常见局面（click 有时会开新窗口），Agent 需要能看见。"""
    try:
        tabs = await session.get_tabs()
    except Exception as exc:  # pragma: no cover - 上游接口差异
        return f"读取标签页失败: {exc}"
    lines = []
    for tab in tabs:
        tab_id = getattr(tab, "tab_id", None) or getattr(tab, "target_id", "")
        url = getattr(tab, "url", "") or ""
        title = getattr(tab, "title", "") or ""
        lines.append(f"- [{tab_id}] {title} {url}".rstrip())
    return "\n".join(lines) or "当前没有打开的标签页。"


# 执行后自动回读页面状态的动作：它们会改变 DOM 或页面位置，不回读的话 Agent
# 只能再花一步 action=state，等于每次交互都多一次往返
_OBSERVE_AFTER = frozenset({"open", "click", "type", "keys", "back", "switch"})
_ALL_ACTIONS = frozenset(REGISTRY_ACTIONS) | {"state", "tabs", "text"}
# 每个动作的必填参数；缺了直接告诉 Agent 该补什么
_REQUIRED = {
    "open": ("url",),
    "click": ("index",),
    "type": ("index", "text"),
    "keys": ("keys",),
    "switch": ("tab_id",),
}


def _build_params(action: str, kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """把工具参数装配成 browser-use 动作需要的字典；缺必填项时返回可行动的提示。"""
    for name in _REQUIRED.get(action, ()):
        if kwargs.get(name) in (None, ""):
            return {}, f"action={action} 缺少必填参数 {name}"
    if action == "open":
        return {"url": str(kwargs["url"]).strip(), "new_tab": bool(kwargs.get("new_tab", False))}, ""
    if action == "click":
        return {"index": int(kwargs["index"])}, ""
    if action == "type":
        return {"index": int(kwargs["index"]), "text": str(kwargs["text"]), "clear": True}, ""
    if action == "scroll":
        return {"down": bool(kwargs.get("down", True)), "pages": float(kwargs.get("pages", 1.0))}, ""
    if action == "keys":
        return {"keys": str(kwargs["keys"])}, ""
    if action == "switch":
        return {"tab_id": str(kwargs["tab_id"])}, ""
    return {}, ""


class BrowserTool:
    """把浏览器动作原语交给 Agent，由它自己决定打开什么、点哪里。

    与旧的 browse 工具的区别：旧版把一句自然语言任务丢给 browser-use 的子 Agent，
    由它再规划一轮——两套提示词、两次 LLM 开销，失败原因被吞在内部（主 Agent 只
    看到一句"浏览器执行出错"，只能猜"连接异常"然后把浏览器整个放弃）。现在直接
    下发动作：主 Agent 看页面状态、自己决定下一步，浏览器侧不需要任何 LLM 调用。
    """

    name = "browser"
    description = (
        "直接操控真实浏览器，用于静态抓取拿不到的页面（要登录态、动态渲染、需要点击展开或翻页）。"
        "先用 action=open 打开网址，返回的文本里每个可交互元素都带编号，之后用编号 click / type。"
        "比 fetch 慢，静态页面优先用 fetch。"
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALL_ACTIONS),
                "description": (
                    "open=打开网址（返回页面元素编号）；state=重看当前页面元素编号；"
                    "click=点击编号；type=向编号输入文本；scroll=滚动；keys=按键或快捷键；"
                    "back=后退；tabs=列标签页；switch=切到某标签页；text=读当前页面正文"
                ),
            },
            "url": {"type": "string", "description": "open 要打开的完整网址"},
            "index": {"type": "integer", "description": "click / type 操作的元素编号，来自 open 或 state 的输出"},
            "text": {"type": "string", "description": "type 要输入的文本"},
            "keys": {"type": "string", "description": "keys 要按的键，如 Enter、Escape、Control+a"},
            "tab_id": {"type": "string", "description": "switch 的目标标签页 id（来自 tabs，形如 ab12）"},
            "down": {"type": "boolean", "description": "scroll 方向，true 向下（默认）"},
            "pages": {"type": "number", "description": "scroll 翻几屏，0.5～10，默认 1"},
        },
        "required": ["action"],
    }

    def __init__(
        self,
        settings: AgentSection,
        *,
        data_dir: Path | None = None,
        runner: BrowserRunner | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._data_dir = data_dir
        self._runner = runner
        self._semaphore = semaphore or asyncio.Semaphore(max(1, settings.browser_concurrency))

    def _resolve_runner(self) -> BrowserRunner:
        """注入优先；否则用配置构造真实驱动（缺 browser-use 时立刻报错，不必等到连接失败）。"""
        if self._runner is not None:
            return self._runner
        if not browser_use_available():
            raise ImportError(INSTALL_HINT)
        if self._data_dir is None:
            raise RuntimeError("未注入浏览器驱动时，必须提供 data_dir")
        # 必须存回实例：否则 aclose() 找不到会话，浏览器进程会残留
        self._runner = _BrowserActions(self._settings, self._data_dir)
        return self._runner

    async def run(self, action: str = "", **kwargs: Any) -> ToolResult:
        action = str(action or "").strip().lower()
        if action not in _ALL_ACTIONS:
            return ToolResult.failure(f"action 必须是 {'、'.join(sorted(_ALL_ACTIONS))} 之一，收到「{action}」")
        params, problem = _build_params(action, kwargs)
        if problem:
            return ToolResult.failure(problem)
        try:
            runner = self._resolve_runner()
        except ImportError:
            return ToolResult.failure(INSTALL_HINT)
        except RuntimeError as exc:
            return ToolResult.failure(str(exc))

        async with self._semaphore:
            outcome = await runner.act(action, params)
            if not outcome.ok:
                return ToolResult.failure(outcome.error)
            body = outcome.text or "动作已执行。"
            if action in _OBSERVE_AFTER:
                # 交互后立刻回读，让 Agent 在同一个观察里看到结果，省一次往返
                state = await runner.act("state", {})
                if state.ok and state.text:
                    body = f"{body}\n\n当前页面：\n{state.text}"
        url = str(params.get("url") or "")
        sources = [Source(title=url, url=url)] if url.startswith("http") else []
        return ToolResult(text=_clip(body), sources=sources)

    async def aclose(self) -> None:
        runner = self._runner
        if runner is None:
            return
        close = getattr(runner, "aclose", None)
        if callable(close):
            await close()
