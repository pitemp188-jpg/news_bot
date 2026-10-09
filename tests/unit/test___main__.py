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
    calls: list[str] = []

    async def fake_login(_store: object, *, base_url: str = "") -> WeixinAccount:
        calls.append(base_url)
        return WeixinAccount(account_id="bot-1", token="tk")

    monkeypatch.setattr(cli, "qr_login", fake_login)
    assert cli.main(["login", "weixin"]) == 0
    out = capsys.readouterr().out
    assert "bot-1" in out
    # 提示里要说明：平台已发凭证时不需要本命令
    assert "直接写进 .env" in out
    assert calls == ["https://ilinkai.weixin.qq.com"]


def test_login_weixin_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_login(_store: object, *, base_url: str = "") -> None:
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


def test_doctor_reports_ok_with_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_PROVIDER", "searxng")
    # 显式清空平台凭证，避免本机 .env 影响断言
    for key in ("QQ_APP_ID", "QQ_CLIENT_SECRET", "WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"):
        monkeypatch.setenv(key, "")
    get_config.cache_clear()
    try:
        assert cli.main(["doctor"]) == 0
    finally:
        get_config.cache_clear()
    out = capsys.readouterr().out
    assert "自检通过" in out
    # 明确说明凭证来自开放平台，运行期不需要登录
    assert "来自各自开放平台" in out
    assert "weixin:  缺失" in out
    assert "qqbot:   缺失" in out


def test_doctor_fails_without_llm_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "")
    get_config.cache_clear()
    try:
        assert cli.main(["doctor"]) == 1
    finally:
        get_config.cache_clear()
    assert "LLM_API_KEY 缺失" in capsys.readouterr().out


def test_doctor_mentions_configured_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QQ_APP_ID", "1905423626")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "bot-1")
    monkeypatch.setenv("WEIXIN_TOKEN", "token-value")
    get_config.cache_clear()
    try:
        assert cli.main(["doctor"]) == 0
    finally:
        get_config.cache_clear()
    out = capsys.readouterr().out
    # 密钥只能显示"已配置"，绝不能打印明文
    assert "secret-value" not in out and "token-value" not in out
    assert "weixin:  .env" in out
    assert "qqbot:   已配置" in out


def test_backup_creates_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_backup(_self: object, out: str | None = None) -> Path:
        assert out is None, "未指定 --out 时应由 App 决定默认目录"
        return tmp_path / "backups" / "newsbot-x.db"

    monkeypatch.setattr(cli.App, "backup", fake_backup.__get__(None, cli.App))
    assert cli.main(["backup"]) == 0
    assert "数据库快照已生成" in capsys.readouterr().out


def test_backup_reports_missing_database(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # 全新部署时数据库还没建；过去会抛 FileNotFoundError 堆栈，这里必须给可操作提示
    async def missing(_self: object, out: str | None = None) -> Path:
        raise FileNotFoundError("数据库不存在：/tmp/newsbot.db")

    monkeypatch.setattr(cli.App, "backup", missing.__get__(None, cli.App))
    assert cli.main(["backup"]) == 1
    out = capsys.readouterr().out
    assert "数据库不存在" in out
    assert "python -m newsbot run" in out
    assert "数据库快照已生成" not in out


def test_api_subcommand_is_registered() -> None:
    args = cli._build_parser().parse_args(["api", "--host", "0.0.0.0", "--port", "9000"])
    assert (args.command, args.host, args.port) == ("api", "0.0.0.0", 9000)

    defaults = cli._build_parser().parse_args(["api"])
    assert defaults.host is None and defaults.port is None
