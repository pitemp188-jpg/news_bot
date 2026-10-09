"""
模块: tests.unit.dispatcher.test_session
职责: 校验会话历史的追加、裁剪、读取与清空
依赖: dispatcher.session, core.db
"""

from __future__ import annotations

from pathlib import Path

from newsbot.core.db import Database
from newsbot.dispatcher.session import SessionStore


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'session.db'}")
    await db.init()
    return db


async def test_unknown_session_returns_empty(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db)
        assert await store.load("qqbot", "unknown") == []
    finally:
        await db.dispose()


async def test_append_and_load_round_trip(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db)
        await store.append("qqbot", "c1", user_text="今天 AI 新闻", reply_text="日报内容", user_id="u1")
        history = await store.load("qqbot", "c1")
        assert history == [
            {"role": "user", "content": "今天 AI 新闻"},
            {"role": "assistant", "content": "日报内容"},
        ]
    finally:
        await db.dispose()


async def test_history_is_trimmed_by_turns(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db, max_turns=2)
        for index in range(5):
            await store.append("qqbot", "c1", user_text=f"问题{index}", reply_text=f"回答{index}")
        history = await store.load("qqbot", "c1")
        assert len(history) == 4
        assert history[0]["content"] == "问题3"
        assert history[-1]["content"] == "回答4"
    finally:
        await db.dispose()


async def test_empty_reply_keeps_single_message(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db)
        await store.append("weixin", "c1", user_text="只有提问", reply_text="")
        assert await store.load("weixin", "c1") == [{"role": "user", "content": "只有提问"}]
    finally:
        await db.dispose()


async def test_clear_removes_history(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db)
        await store.append("qqbot", "c1", user_text="a", reply_text="b")
        await store.clear("qqbot", "c1")
        assert await store.load("qqbot", "c1") == []
    finally:
        await db.dispose()


async def test_sessions_are_isolated_per_target(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        store = SessionStore(db)
        await store.append("qqbot", "c1", user_text="a", reply_text="b")
        await store.append("weixin", "c1", user_text="x", reply_text="y")
        assert (await store.load("qqbot", "c1"))[0]["content"] == "a"
        assert (await store.load("weixin", "c1"))[0]["content"] == "x"
    finally:
        await db.dispose()
