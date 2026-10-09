"""
模块: tests.unit.agent.test_browser
职责: 校验浏览器工具的降级提示、替身注入、并发限制与错误处理
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
from newsbot.agent.tools.browser import INSTALL_HINT, BrowserTool
from newsbot.core.config import AgentSection, Secrets


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


# ── 真实运行器的按需构造（browser-use 为可选依赖）──


class _RecordingRunner:
    """记录构造次数、运行次数与关闭次数，替代真实 browser-use 运行器。"""

    instances: ClassVar[list[_RecordingRunner]] = []

    def __init__(self, settings: AgentSection, secrets: object, data_dir: Path) -> None:
        self.settings = settings
        self.secrets = secrets
        self.data_dir = data_dir
        self.runs = 0
        self.closed = 0
        _RecordingRunner.instances.append(self)

    async def run(self, url: str, task: str) -> str:
        self.runs += 1
        return f"{url} 的正文"

    async def aclose(self) -> None:
        self.closed += 1


def _secrets() -> Secrets:
    return Secrets(_env_file=None, llm_api_key="sk-test", llm_model="writing-model")


@pytest.fixture(autouse=True)
def _reset_instances() -> Iterator[None]:
    _RecordingRunner.instances.clear()
    yield
    _RecordingRunner.instances.clear()


async def test_real_runner_is_built_once_and_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 按需构造的运行器必须存回实例：否则 aclose() 找不到它，
    # 真实场景下浏览器进程就会残留（这是曾经踩过的坑）
    monkeypatch.setattr(browser_module, "browser_use_available", lambda: True)
    monkeypatch.setattr(browser_module, "_BrowserUseRunner", _RecordingRunner)

    tool = BrowserTool(_settings(), secrets=_secrets(), data_dir=tmp_path)
    await tool.run("https://news.example/a", "提取要点")
    await tool.run("https://news.example/b", "提取要点")

    assert len(_RecordingRunner.instances) == 1, "多次调用应复用同一个运行器"
    runner = _RecordingRunner.instances[0]
    assert runner.runs == 2
    assert runner.data_dir == tmp_path

    await tool.aclose()
    assert runner.closed == 1, "关闭时必须回收运行器"


async def test_runner_uses_browser_model_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 浏览器子 Agent 对速度与 JSON 稳定性要求更高，允许单独指定模型
    monkeypatch.setattr(browser_module, "browser_use_available", lambda: True)
    monkeypatch.setattr(browser_module, "_BrowserUseRunner", _RecordingRunner)

    secrets = Secrets(_env_file=None, llm_api_key="sk-test", llm_model="writing-model")
    tool = BrowserTool(_settings(), secrets=secrets, data_dir=tmp_path)
    await tool.run("https://news.example/a")
    assert _RecordingRunner.instances[0].secrets.llm_model_browser == ""
    assert _RecordingRunner.instances[0].settings.browser_max_steps != _settings().max_steps


async def test_missing_secrets_is_reported_without_injection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "browser_use_available", lambda: True)
    tool = BrowserTool(_settings())
    result = await tool.run("https://news.example/a")
    assert "secrets" in result.text
    await tool.aclose()


async def test_injected_runner_receives_task_text(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []

    class _CaptureRunner:
        async def run(self, url: str, task: str) -> str:
            captured.append((url, task))
            return "正文"

    tool = BrowserTool(_settings(), secrets=_secrets(), data_dir=tmp_path, runner=_CaptureRunner())
    await tool.run("https://news.example/a", "提取要点")
    assert captured == [("https://news.example/a", "提取要点")]
    await tool.aclose()


async def test_task_contract_and_options_reach_browser_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """约束与关键参数必须真的传给 browser-use，不能只写在常量里。"""
    captured: dict[str, Any] = {}

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent"] = kwargs

        async def run(self, max_steps: int = 10) -> Any:
            captured["max_steps"] = max_steps

            class _History:
                def final_result(self) -> str:
                    return "结论文本"

            return _History()

    class _FakeProfile:
        def __init__(self, **kwargs: Any) -> None:
            captured["profile"] = kwargs

    class _FakeSession:
        def __init__(self, browser_profile: Any = None) -> None:
            self.browser_profile = browser_profile

        async def start(self) -> None:
            captured["started"] = True

        async def stop(self) -> None:
            captured["stopped"] = True

    class _FakeChat:
        def __init__(self, **kwargs: Any) -> None:
            captured["llm"] = kwargs

    module = types.ModuleType("browser_use")
    module.Agent = _FakeAgent
    module.BrowserProfile = _FakeProfile
    module.BrowserSession = _FakeSession
    llm_module = types.ModuleType("browser_use.llm")
    llm_module.ChatOpenAI = _FakeChat
    module.llm = llm_module
    monkeypatch.setitem(sys.modules, "browser_use", module)
    monkeypatch.setitem(sys.modules, "browser_use.llm", llm_module)

    settings = _settings(browser_max_steps=7, browser_timeout_seconds=90, browser_headless=False)
    runner = browser_module._BrowserUseRunner(settings, _secrets(), tmp_path)

    text = await runner.run("https://news.example/a", "提取要点")
    assert text == "结论文本"

    task = captured["agent"]["task"]
    assert "https://news.example/a" in task and "提取要点" in task
    assert browser_module.TASK_SUFFIX in task, "反编造约束必须进入子 Agent 的任务"
    assert captured["agent"]["use_vision"] is False, "不支持视觉的模型不能传截图"
    assert captured["agent"]["llm_timeout"] == 90
    assert captured["max_steps"] == 7, "应使用浏览器专用步数预算"
    assert captured["profile"]["enable_default_extensions"] is False, "跳过扩展下载才能快速启动"
    assert captured["profile"]["headless"] is False
    assert str(tmp_path) in captured["profile"]["user_data_dir"]

    await runner.aclose()
    assert captured["stopped"] is True


async def test_start_failure_leaves_no_stale_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动失败后不能留下半死会话，否则后续调用会一直复用它。"""
    attempts: list[int] = []

    class _FailingSession:
        def __init__(self, browser_profile: Any = None) -> None:
            self.stopped = 0

        async def start(self) -> None:
            attempts.append(1)
            raise RuntimeError("EventBus timed out")

        async def stop(self) -> None:
            self.stopped += 1

    module = types.ModuleType("browser_use")
    module.Agent = object
    module.BrowserProfile = lambda **kwargs: kwargs
    module.BrowserSession = _FailingSession
    monkeypatch.setitem(sys.modules, "browser_use", module)

    runner = browser_module._BrowserUseRunner(_settings(), _secrets(), tmp_path)
    with pytest.raises(RuntimeError, match="浏览器启动失败"):
        await runner.run("https://news.example/a", "提取")
    assert runner._session is None, "失败的会话不能被缓存"

    # 第二次调用必须重新尝试启动，而不是直接复用坏会话
    with pytest.raises(RuntimeError, match="浏览器启动失败"):
        await runner.run("https://news.example/a", "提取")
    assert len(attempts) == 2
