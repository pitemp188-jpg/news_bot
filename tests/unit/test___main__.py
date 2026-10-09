"""
模块: tests.unit.test___main__
职责: 校验命令行入口的参数解析与各子命令分支
依赖: newsbot.__main__
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot import __main__ as cli
from newsbot.core.config import get_config
from newsbot.gateway.weixin import WeixinAccount


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def test_parser_requires_command() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["login", "telegram"])


def test_login_weixin_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    async def fake_login(_store: object) -> WeixinAccount:
        return WeixinAccount(account_id="bot-1", token="tk")

    monkeypatch.setattr(cli, "qr_login", fake_login)
    assert cli.main(["login", "weixin"]) == 0
    assert "bot-1" in capsys.readouterr().out


def test_login_weixin_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_login(_store: object) -> None:
        return None

    monkeypatch.setattr(cli, "qr_login", fake_login)
    assert cli.main(["login", "weixin"]) == 1


def test_notify_reports_when_no_target(capsys: pytest.CaptureFixture[str]) -> None:
    # 未配置平台凭证也没有订阅时，应给出明确提示而不是静默成功
    assert cli.main(["notify", "长跑完成"]) == 1
    assert "没有可用的推送目标" in capsys.readouterr().out


def test_notify_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeApp:
        def __init__(self, _config: object) -> None:
            self.calls: list[tuple[str, str | None, str | None]] = []

        async def start(self) -> dict[str, bool]:
            return {}

        async def stop(self) -> None:
            return None

        async def notify(self, text: str, *, platform: str | None = None, chat_id: str | None = None) -> bool:
            self.calls.append((text, platform, chat_id))
            return True

    monkeypatch.setattr(cli, "App", FakeApp)
    assert cli.main(["notify", "上线了", "--platform", "weixin", "--chat-id", "u1"]) == 0
    assert "已推送" in capsys.readouterr().out


def test_run_delegates_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_service() -> int:
        return 0

    monkeypatch.setattr(cli, "run_service", fake_service)
    assert cli.main(["run"]) == 0
