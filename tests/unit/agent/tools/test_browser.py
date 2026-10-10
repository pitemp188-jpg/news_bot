"""
模块: tests.unit.agent.tools.test_browser
职责: 校验浏览器工具的动作下发、参数校验、错误分类、就绪探测与并发限制
依赖: agent.tools.browser
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from tests.fakes.search import FakeBrowserRunner

from newsbot.agent.tools import browser as browser_module
from newsbot.agent.tools.browser import (
    ActionOutcome,
    BrowserTool,
    classify_error,
)
from newsbot.core.config import AgentSection


def _settings(**overrides: object) -> AgentSection:
    return AgentSection(**overrides)  # type: ignore[arg-type]


async def test_open_sends_navigate_and_refetches_state() -> None:
    runner = FakeBrowserRunner(text="已打开", state="[3] 链接 全部新闻")
    tool = BrowserTool(_settings(), runner=runner)
    result = await tool.run("open", url="https://news.example/a")

    actions = [name for name, _ in runner.calls]
    assert actions == ["open", "state"], "交互后必须自动回读页面，否则 Agent 还要多花一步"
    assert runner.calls[0][1] == {"url": "https://news.example/a", "new_tab": False}
    assert "已打开" in result.text and "[3] 链接 全部新闻" in result.text
    # 打开过的网址要进来源列表，Agent 才知道自己引用的是哪一页
    assert result.sources[0].url == "https://news.example/a"
    await tool.aclose()


async def test_state_action_does_not_refetch() -> None:
    runner = FakeBrowserRunner(state="[1] 按钮 展开")
    tool = BrowserTool(_settings(), runner=runner)
    result = await tool.run("state")
    assert [name for name, _ in runner.calls] == ["state"], "state 不该再回读一次"
    assert "[1] 按钮 展开" in result.text
    await tool.aclose()


async def test_click_passes_index_and_observes() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    await tool.run("click", index=12)
    assert runner.calls[0] == ("click", {"index": 12})
    assert [name for name, _ in runner.calls] == ["click", "state"]
    await tool.aclose()


async def test_type_clears_field_by_default() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    await tool.run("type", index=4, text="具身智能")
    assert runner.calls[0] == ("type", {"index": 4, "text": "具身智能", "clear": True})
    await tool.aclose()


async def test_scroll_defaults_down_one_page() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    await tool.run("scroll")
    assert runner.calls[0] == ("scroll", {"down": True, "pages": 1.0})
    # 滚动不改变元素编号，不该再回读一次
    assert [name for name, _ in runner.calls] == ["scroll"]
    await tool.aclose()


async def test_tabs_and_switch_use_short_ids() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    await tool.run("tabs")
    await tool.run("switch", tab_id="ab12")
    assert runner.calls[0][0] == "tabs"
    assert runner.calls[1] == ("switch", {"tab_id": "ab12"})
    await tool.aclose()


@pytest.mark.parametrize(
    ("action", "kwargs", "missing"),
    [
        ("open", {}, "url"),
        ("click", {}, "index"),
        ("type", {"index": 1}, "text"),
        ("keys", {}, "keys"),
        ("switch", {}, "tab_id"),
    ],
)
async def test_required_parameters_are_enforced(action: str, kwargs: dict[str, Any], missing: str) -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    result = await tool.run(action, **kwargs)
    assert f"缺少必填参数 {missing}" in result.text
    assert runner.calls == [], "参数不合法时不该打扰浏览器"
    await tool.aclose()


async def test_unknown_action_is_rejected_before_touching_browser() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(), runner=runner)
    result = await tool.run("fly")
    assert "action 必须是" in result.text
    assert runner.calls == []
    await tool.aclose()


async def test_driver_error_outcome_is_surfaced() -> None:
    # 失败说明必须原样传给 Agent：它只能靠这段文字决定重试还是换工具
    reason = "click 失败：浏览器会话已断开（已丢弃，下次调用会自动重建，可直接重试）。"
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(error=reason))
    result = await tool.run("click", index=1)
    assert reason in result.text
    await tool.aclose()


async def test_missing_dependency_reports_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(browser_module, "browser_use_available", lambda: False)
    tool = BrowserTool(_settings(), data_dir=tmp_path)
    result = await tool.run("state")
    assert browser_module.INSTALL_HINT in result.text
    await tool.aclose()


async def test_missing_data_dir_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "browser_use_available", lambda: True)
    tool = BrowserTool(_settings())
    result = await tool.run("state")
    assert "data_dir" in result.text
    await tool.aclose()


async def test_long_output_is_truncated() -> None:
    tool = BrowserTool(_settings(), runner=FakeBrowserRunner(state="长" * 9000))
    result = await tool.run("state")
    assert "内容过长已截断" in result.text
    await tool.aclose()


async def test_concurrency_is_limited() -> None:
    runner = FakeBrowserRunner()
    tool = BrowserTool(_settings(browser_concurrency=1), runner=runner)
    await asyncio.gather(*(tool.run("open", url=f"https://news.example/{i}") for i in range(4)))
    assert runner.peak == 1
    await tool.aclose()


# ── 错误分类：把底层异常翻译成 Agent 能据此决策的说明 ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (RuntimeError("CDP client not initialized"), "会话已断开"),
        (RuntimeError("403 Forbidden"), "拒绝访问"),
        (TimeoutError("navigation timed out"), "超时"),
        (ValueError("index 99 out of range"), "元素编号不存在"),
        (RuntimeError("something odd"), "something odd"),
    ],
)
def test_classify_error_gives_actionable_reason(raw: Exception, expected: str) -> None:
    assert expected in classify_error("click", raw)


def test_action_outcome_ok_flag() -> None:
    assert ActionOutcome(text="x").ok is True
    assert ActionOutcome(error="y").ok is False


# ── 驱动层：动作派发、就绪探测、会话重建（用假的 browser_use 模块离线验证）──


class _FakeResult:
    def __init__(self, *, extracted_content: str | None = None, error: str | None = None) -> None:
        self.extracted_content = extracted_content
        self.long_term_memory = None
        self.error = error


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.registry = types.SimpleNamespace(actions={"click": object()})
        self.error: Exception | None = None

    async def execute_action(self, name: str, params: dict[str, Any], browser_session: Any = None) -> Any:
        if self.error is not None:
            raise self.error
        self.calls.append((name, params, browser_session))
        return _FakeResult(extracted_content=f"{name} 完成")


class _FakeTools:
    instances: ClassVar[list[_FakeTools]] = []

    def __init__(self) -> None:
        self.registry = _FakeRegistry()
        _FakeTools.instances.append(self)


class _FakeSession:
    """假的 BrowserSession。

    `ready_after` / `start_error` 是**类级**开关，测试在构造前设置；不要放进
    __init__，否则实例属性会盖掉类属性，测试设的值就失效了。
    """

    instances: ClassVar[list[_FakeSession]] = []
    ready_after = 0  # 前 N 次取页面状态失败，用来验证"启动后要等真的能接受指令"
    start_error: Exception | None = None
    state_error: Exception | None = None  # 取状态时抛错，验证连接断开时会丢弃会话
    page_text: ClassVar[str | None] = "正文第一段\n\n正文第二段"
    pages: ClassVar[list[_FakePage]] = []

    def __init__(self, *, browser_profile: Any = None) -> None:
        self.browser_profile = browser_profile
        self.start_calls = 0
        self.stop_calls = 0
        self.state_calls = 0
        _FakeSession.instances.append(self)

    async def start(self) -> None:
        self.start_calls += 1
        if _FakeSession.start_error is not None:
            raise _FakeSession.start_error

    async def stop(self) -> None:
        self.stop_calls += 1

    async def get_state_as_text(self) -> str:
        self.state_calls += 1
        if _FakeSession.state_error is not None:
            raise _FakeSession.state_error
        if self.state_calls <= _FakeSession.ready_after:
            raise RuntimeError("CDP client not initialized - browser may not be connected yet")
        return "[1] 链接 首页"

    async def get_tabs(self) -> list[Any]:
        tab = types.SimpleNamespace(tab_id="ab12", url="https://news.example/a", title="首页")
        return [tab]

    async def get_current_page(self) -> Any:
        if _FakeSession.page_text is None:
            return None
        page = _FakePage(_FakeSession.page_text)
        _FakeSession.pages.append(page)
        return page


class _FakePage:
    def __init__(self, text: str, *, error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.js: list[str] = []

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.js.append(script)
        if self._error is not None:
            raise self._error
        return self._text


@pytest.fixture
def fake_browser_use(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入假的 browser_use 模块：驱动层是唯一需要它的地方。"""
    _FakeTools.instances.clear()
    _FakeSession.instances.clear()
    _FakeSession.ready_after = 0
    _FakeSession.start_error = None
    _FakeSession.state_error = None
    _FakeSession.page_text = "正文第一段\n\n正文第二段"
    _FakeSession.pages.clear()
    module = types.ModuleType("browser_use")
    module.BrowserProfile = lambda **kwargs: kwargs
    module.BrowserSession = _FakeSession
    service = types.ModuleType("browser_use.tools.service")
    service.Tools = _FakeTools
    monkeypatch.setitem(sys.modules, "browser_use", module)
    monkeypatch.setitem(sys.modules, "browser_use.tools.service", service)
    yield
    _FakeTools.instances.clear()
    _FakeSession.instances.clear()
    _FakeSession.pages.clear()


async def test_driver_dispatches_registry_actions(fake_browser_use: None, tmp_path: Path) -> None:
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("click", {"index": 3})
    assert outcome.text == "click 完成"
    name, params, session = _FakeTools.instances[0].registry.calls[0]
    assert name == "click", "动作名要映射到 browser-use 注册表里的名字"
    assert params == {"index": 3}
    assert session is _FakeSession.instances[0]
    await driver.aclose()


async def test_driver_profile_flags_and_data_dir(fake_browser_use: None, tmp_path: Path) -> None:
    driver = browser_module._BrowserActions(_settings(browser_headless=False), tmp_path)
    await driver.act("state", {})
    profile = _FakeSession.instances[0].browser_profile
    assert profile["headless"] is False
    assert profile["enable_default_extensions"] is False, "跳过扩展下载才能快速启动（实测曾拖过 30s）"
    assert str(tmp_path) in profile["user_data_dir"]


async def test_driver_waits_until_session_is_ready(fake_browser_use: None, tmp_path: Path) -> None:
    # session.start() 返回不代表能接受指令：实测紧接着下发动作会报 CDP 未初始化
    _FakeSession.ready_after = 1
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("state", {})
    assert outcome.ok, outcome.error
    # 探测 1 次失败 + 重试 1 次 + 本次 state 动作本身 1 次
    assert _FakeSession.instances[0].state_calls == 3, "就绪探测失败后必须重试"
    await driver.aclose()


async def test_driver_start_failure_leaves_no_session(fake_browser_use: None, tmp_path: Path) -> None:
    _FakeSession.start_error = RuntimeError("EventBus timed out")
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("state", {})
    assert "浏览器启动失败" in outcome.error
    assert driver._session is None, "失败的会话不能被缓存"
    failed, _ = _FakeSession.instances[0], _FakeSession.instances[0].stop_calls
    assert failed.stop_calls == 1, "启动失败也要尽力清理"

    # 第二次调用必须重新尝试，而不是复用坏会话
    await driver.act("state", {})
    assert len(_FakeSession.instances) == 2
    await driver.aclose()


async def test_driver_drops_dead_session_so_next_call_rebuilds(fake_browser_use: None, tmp_path: Path) -> None:
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    await driver.act("state", {})
    _FakeTools.instances[0].registry.error = RuntimeError("CDP client not initialized")
    outcome = await driver.act("click", {"index": 1})
    assert "会话已断开" in outcome.error
    assert driver._session is None, "死连接必须丢弃，否则后续每个动作都以同样错误失败"

    _FakeTools.instances[0].registry.error = None
    assert (await driver.act("state", {})).ok
    assert len(_FakeSession.instances) == 2, "下次调用应当重建会话"
    await driver.aclose()


async def test_driver_reuses_session_and_registry(fake_browser_use: None, tmp_path: Path) -> None:
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    await driver.act("state", {})
    await driver.act("click", {"index": 1})
    assert len(_FakeSession.instances) == 1
    assert _FakeSession.instances[0].start_calls == 1
    assert len(_FakeTools.instances) == 1, "动作注册表不该反复重建"
    await driver.aclose()
    assert _FakeSession.instances[0].stop_calls == 1


async def test_driver_lists_tabs_directly(fake_browser_use: None, tmp_path: Path) -> None:
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("tabs", {})
    assert "ab12" in outcome.text and "首页" in outcome.text
    assert _FakeTools.instances[0].registry.calls == [], "tabs 是会话直调，不走动作注册表"
    await driver.aclose()


async def test_driver_timeout_is_reported(
    fake_browser_use: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _HangingRegistry(_FakeRegistry):
        async def execute_action(self, name: str, params: dict[str, Any], browser_session: Any = None) -> Any:
            await asyncio.sleep(5)
            return _FakeResult()

    monkeypatch.setattr(_FakeTools, "__init__", lambda self: setattr(self, "registry", _HangingRegistry()))
    driver = browser_module._BrowserActions(_settings(browser_timeout_seconds=1), tmp_path)
    outcome = await driver.act("click", {"index": 1})
    assert "超过 1s 未完成" in outcome.error
    await driver.aclose()


async def test_driver_reads_page_text_without_extraction_llm(fake_browser_use: None, tmp_path: Path) -> None:
    # 不能用 browser-use 的 read_long_content：它要求传抽取模型，而浏览器侧刻意不接 LLM
    # （实测会返回「requires page_extraction_llm but none provided」）
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("text", {})
    assert outcome.ok, outcome.error
    assert "正文第一段" in outcome.text and "正文第二段" in outcome.text
    assert _FakeTools.instances[0].registry.calls == [], "取正文是会话直调，不能走动作注册表"
    assert _FakeSession.pages[0].js[0].startswith("("), "CDP evaluate 的脚本必须以函数表达式开头"
    await driver.aclose()


async def test_driver_reports_empty_page_text(fake_browser_use: None, tmp_path: Path) -> None:
    _FakeSession.page_text = None
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("text", {})
    assert outcome.ok and "没有可读的页面" in outcome.text
    await driver.aclose()


async def test_driver_collapses_blank_lines_in_page_text(fake_browser_use: None, tmp_path: Path) -> None:
    _FakeSession.page_text = "第一行\n\n\n第二行"
    driver = browser_module._BrowserActions(_settings(), tmp_path)
    outcome = await driver.act("text", {})
    assert outcome.text == "第一行\n第二行", "空行要压掉，否则一屏塞不下几条信息"
    await driver.aclose()


def test_action_count_handles_two_level_registry() -> None:
    # 真实结构是 tools.registry（Registry）.registry（ActionRegistry）.actions；
    # 写错一层只会抛 AttributeError，实测因此让整个 open 失败
    assert browser_module._action_count(_FakeTools()) == 1
    flat = types.SimpleNamespace(registry=types.SimpleNamespace(actions={"a": 1, "b": 2}))
    assert browser_module._action_count(flat) == 2, "只有一层时也应能数出来"
    assert browser_module._action_count(types.SimpleNamespace()) == 0
