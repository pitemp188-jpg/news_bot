"""
模块: tests.unit.test_app
职责: 校验服务组装——平台选择、默认订阅、定时触发、主动推送与关闭顺序
依赖: newsbot.app
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter
from tests.fakes.llm import FakeLLM, reply

from newsbot.app import App
from newsbot.core.config import Config, ScheduleSection, Secrets
from newsbot.core.models import Schedule, Task

pytestmark = pytest.mark.integration


def _config(tmp_path: Path, **schedule: object) -> Config:
    secrets = Secrets(_env_file=None, llm_api_key="sk-test", weixin_allowed_users="u1")
    return Config(
        secrets=secrets,
        schedule=ScheduleSection(**schedule),  # type: ignore[arg-type]
        data_dir=tmp_path,
    )


async def _app(tmp_path: Path, adapter: FakeAdapter | None = None, **schedule: object) -> tuple[App, FakeAdapter]:
    config = _config(tmp_path, **schedule)
    fake = adapter or FakeAdapter(platform="weixin")
    app = App(config, llm=FakeLLM(replies=[reply("结果 [1]")]), adapters=[fake])
    await app.start()
    return app, fake


async def _drain(app: App, timeout: float = 5.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    assert app.db is not None
    while loop.time() < deadline:
        async with app.db.session() as session:
            tasks = list((await session.execute(select(Task))).scalars())
        if tasks and all(task.status in {"succeeded", "failed", "timeout", "cancelled"} for task in tasks):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("任务未在超时时间内结束")


async def test_start_registers_adapters_and_stops(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path)
    try:
        assert app.router is not None and app.router.platforms == ["weixin"]
        assert adapter.connect_calls == 1
        assert app.scheduler is not None and app.scheduler.job_ids() == set()
    finally:
        await app.stop()
    assert adapter.disconnect_calls == 1


async def test_first_message_creates_default_schedule_and_replies(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path, default_time="21:00", default_topics=["AI"])
    try:
        await adapter.emit("你好", chat_id="u1", user_id="u1")
        assert app.db is not None
        async with app.db.session() as session:
            rows = list((await session.execute(select(Schedule))).scalars())
        assert [(row.cron, row.topics, row.platform, row.chat_id) for row in rows] == [
            ("0 21 * * *", ["AI"], "weixin", "u1")
        ]
        assert app.scheduler is not None
        assert app.scheduler.job_ids() == {f"schedule-{rows[0].id}"}
        assert adapter.sent and "已默认开启每日 21:00" in adapter.sent[0][1]
        assert "已收到" in adapter.sent[0][1]

        # 第二条消息不再重复建订阅
        await adapter.emit("再问一次", chat_id="u1", user_id="u1")
        async with app.db.session() as session:
            assert len(list((await session.execute(select(Schedule))).scalars())) == 1
    finally:
        await app.stop()


async def test_unauthorized_user_is_ignored(tmp_path: Path) -> None:
    adapter = FakeAdapter(platform="weixin")
    app, _ = await _app(tmp_path, adapter)
    try:
        await adapter.emit("帮我查点东西", chat_id="stranger", user_id="stranger")
        assert adapter.sent == []
        assert app.db is not None
        async with app.db.session() as session:
            assert list((await session.execute(select(Task))).scalars()) == []
    finally:
        await app.stop()


async def test_scheduled_job_submits_and_delivers(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path)
    try:
        await adapter.emit("/帮助", chat_id="u1", user_id="u1")
        assert app.db is not None and app.scheduler is not None
        async with app.db.session() as session:
            row = (await session.execute(select(Schedule))).scalar_one()

        await app.scheduler.run_job(row.id)
        await _drain(app)

        async with app.db.session() as session:
            task = (await session.execute(select(Task))).scalar_one()
        assert task.kind == "scheduled" and task.chat_id == "u1"
        assert "AI" in task.query
        assert any("结果 [1]" in text for _chat, text, _reply in adapter.sent)
    finally:
        await app.stop()


async def test_scheduled_job_skips_disabled(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path)
    try:
        await adapter.emit("/帮助", chat_id="u1", user_id="u1")
        assert app.db is not None and app.scheduler is not None
        async with app.db.session() as session:
            row = (await session.execute(select(Schedule))).scalar_one()
            row.enabled = False
            await session.commit()
        await app.scheduler.run_job(row.id)
        async with app.db.session() as session:
            assert list((await session.execute(select(Task))).scalars()) == []
    finally:
        await app.stop()


async def test_notify_broadcasts_to_subscribed_chats(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path)
    try:
        assert await app.notify("无人订阅") is False
        await adapter.emit("/帮助", chat_id="u1", user_id="u1")
        adapter.sent.clear()
        assert await app.notify("集合啦") is True
        assert adapter.sent[-1][0] == "u1"
        assert "集合啦" in adapter.sent[-1][1]
        assert await app.notify("定向", platform="weixin", chat_id="u2") is True
        assert adapter.sent[-1][0] == "u2"
    finally:
        await app.stop()


async def test_manual_task_runs_through_pipeline(tmp_path: Path) -> None:
    app, adapter = await _app(tmp_path)
    try:
        assert app.db is not None
        assert await app.targets() == []
        assert app.queue is not None
        await app.queue.submit(kind="manual", query="手动跑一次", platform="weixin", chat_id="u1")
        await _drain(app)
        assert any("结果 [1]" in text for _chat, text, _reply in adapter.sent)
    finally:
        await app.stop()
