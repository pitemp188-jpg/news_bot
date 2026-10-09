"""
模块: tests.unit.test___main__
职责: 校验命令行入口的参数解析与各子命令分支
依赖: newsbot.__main__
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot import __main__ as cli
from newsbot.gateway.weixin import WeixinAccount


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "")


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


def test_notify_prints_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["notify", "长跑完成"]) == 0
    assert "长跑完成" in capsys.readouterr().out


def test_run_reports_not_assembled(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run"]) == 2
    assert "M6" in capsys.readouterr().out
