"""
模块: tests.unit.core.test_config
职责: 校验配置加载、环境变量优先级、数据目录与必填校验
依赖: core.config
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.core.config import FeedSource, SearchSection, default_feeds, load_config
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


def test_default_feeds_carry_weights_and_industries() -> None:
    """默认源要带上质量权重与行业标签，并按行业覆盖多个领域。"""
    feeds = default_feeds()
    assert len(feeds) >= 10, "按行业补源意味着源要有一定覆盖面"
    assert all(feed.url.startswith("https://") for feed in feeds)
    assert any(feed.weight > 1.0 for feed in feeds), "必须存在权重高于基准的权威源"
    assert any(feed.weight < 1.0 for feed in feeds), "也要有降权的源，否则权重没有区分度"
    topics = {topic for feed in feeds for topic in feed.topics}
    for expected in ("AI", "开发", "科技", "芯片"):
        assert expected in topics, f"缺少 {expected} 行业的源"
    assert len({feed.url for feed in feeds}) == len(feeds), "同一个源只该出现一次"


def test_search_section_accepts_plain_url_list() -> None:
    """config.yaml 里只写网址要能继续工作，否则老配置一升级就报错。"""
    section = SearchSection(feeds=["https://a.example/feed", "https://b.example/feed"])
    assert [feed.url for feed in section.feeds] == ["https://a.example/feed", "https://b.example/feed"]
    assert [feed.weight for feed in section.feeds] == [1.0, 1.0]
    assert section.feed_weights() == {"a.example": 1.0, "b.example": 1.0}


def test_search_section_keeps_explicit_weight_and_topics() -> None:
    section = SearchSection(feeds=[{"url": "https://www.a.example/feed", "weight": 1.2, "topics": ["AI"]}])
    assert section.feeds == [FeedSource(url="https://www.a.example/feed", weight=1.2, topics=["AI"])]
    assert section.feed_weights() == {"a.example": 1.2}, "域名要去掉 www.，否则权重匹配不上"
