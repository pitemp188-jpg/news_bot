"""
模块: tests.unit.core.test_config
职责: 校验配置加载、环境变量优先级、数据目录与必填校验
依赖: core.config
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.core.config import load_config
from newsbot.core.errors import ConfigError


def test_defaults_without_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path / "data"))
    config = load_config(tmp_path / "missing.yaml")
    assert config.queue.concurrency == 2
    assert config.schedule.default_time == "21:00"
    assert config.data_dir == tmp_path / "data"


def test_yaml_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("queue:\n  concurrency: 5\nagent:\n  max_steps: 20\n", encoding="utf-8")
    config = load_config(config_file)
    assert config.queue.concurrency == 5
    assert config.agent.max_steps == 20


def test_env_beats_yaml_and_unknown_keys_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "deepseek-reasoner")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("whatever: 1\n", encoding="utf-8")
    config = load_config(config_file)
    assert config.secrets.llm_model == "deepseek-reasoner"


def test_db_url_uses_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    config = load_config(tmp_path / "missing.yaml")
    assert config.db_url.startswith("sqlite+aiosqlite:///")
    assert config.db_url.endswith("newsbot.db")


def test_ensure_ready_rejects_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "")
    config = load_config(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        config.ensure_ready()


def test_ensure_ready_passes_with_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "test-value")
    config = load_config(tmp_path / "missing.yaml")
    config.ensure_ready()
