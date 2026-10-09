"""
模块: tests.integration.test_migrations
职责: 校验 Alembic 首个迁移可以升级到 head 并回滚到 base
依赖: core.config, core.models
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {"task", "schedule", "chat_session", "news_item", "report", "delivery", "llm_usage"}


def _alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _table_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_then_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "newsbot.db"
    config = _alembic_config()

    command.upgrade(config, "head")
    assert _table_names(db_path) >= EXPECTED_TABLES

    command.downgrade(config, "base")
    assert not EXPECTED_TABLES & _table_names(db_path)
