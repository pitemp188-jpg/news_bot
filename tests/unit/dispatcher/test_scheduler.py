"""
模块: tests.unit.dispatcher.test_scheduler
职责: 校验定时作业同步、cron 解析、触发与失效订阅跳过
依赖: dispatcher.scheduler, core.db
"""

from __future__ import annotations

from pathlib import Path

from newsbot.core.db import Database
from newsbot.core.models import Schedule
from newsbot.dispatcher.scheduler import REFRESH_JOB_ID, Scheduler, job_id


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}")
    await db.init()
    return db


def _collector() -> tuple[list[int], object]:
    submitted: list[int] = []

    async def submit(row: Schedule) -> None:
        submitted.append(row.id)

    return submitted, submit


async def test_sync_only_adds_enabled(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add_all(
                [
                    Schedule(cron="0 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1"),
                    Schedule(cron="0 8 * * *", topics=["AI"], platform="qqbot", chat_id="c2", enabled=False),
                ]
            )
            await session.commit()
        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.sync()
        assert scheduler.job_ids() == {job_id(1)}
    finally:
        await db.dispose()


async def test_sync_removes_stale_jobs(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add(Schedule(cron="0 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1"))
            await session.commit()
        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.sync()
        assert scheduler.job_ids() == {job_id(1)}

        async with db.session() as session:
            row = await session.get(Schedule, 1)
            assert row is not None
            row.enabled = False
            await session.commit()
        await scheduler.sync()
        assert scheduler.job_ids() == set()
    finally:
        await db.dispose()


async def test_cron_fields_are_parsed(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add(Schedule(cron="30 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1"))
            await session.commit()
        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.sync()
        job = scheduler._scheduler.get_job(job_id(1))
        assert job is not None
        fields = {field.name: str(field) for field in job.trigger.fields}  # type: ignore[attr-defined]
        assert fields["hour"] == "21"
        assert fields["minute"] == "30"
    finally:
        await db.dispose()


async def test_run_job_submits_enabled_schedule(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add(Schedule(cron="0 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1"))
            await session.commit()
        submitted, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.run_job(1)
        assert submitted == [1]
    finally:
        await db.dispose()


async def test_run_job_skips_disabled_or_missing(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        async with db.session() as session:
            session.add(Schedule(cron="0 21 * * *", topics=["AI"], platform="qqbot", chat_id="c1", enabled=False))
            await session.commit()
        submitted, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.run_job(1)
        await scheduler.run_job(999)
        assert submitted == []
    finally:
        await db.dispose()


async def test_start_and_shutdown(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        scheduler.start()
        assert scheduler._scheduler.running
        await scheduler.shutdown()
        assert not scheduler._scheduler.running
    finally:
        await db.dispose()


# ── 事件层刷新 ──


async def test_sync_registers_refresh_job_without_any_schedule(tmp_path: Path) -> None:
    """刷新作业与订阅无关：一个订阅都没有时也要挂上，到点自己跑。"""
    db = await _make_db(tmp_path)
    try:
        calls: list[int] = []

        async def refresh() -> None:
            calls.append(1)

        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit, refresh=refresh, refresh_interval=30)
        await scheduler.sync()
        assert scheduler.job_ids() == {REFRESH_JOB_ID}
        job = scheduler._scheduler.get_job(REFRESH_JOB_ID)
        assert job is not None and str(job.trigger) == "interval[0:30:00]"
    finally:
        await db.dispose()


async def test_sync_omits_refresh_job_when_interval_disabled(tmp_path: Path) -> None:
    """间隔配成 0（或没接回调）时不挂作业，也不能留下上一次同步的僵尸作业。"""
    db = await _make_db(tmp_path)
    try:

        async def refresh() -> None:  # pragma: no cover — 不该被调用
            raise AssertionError("间隔为 0 时不应触发刷新")

        _, submit = _collector()
        on = Scheduler(db, timezone="Asia/Shanghai", submit=submit, refresh=refresh, refresh_interval=30)
        await on.sync()
        assert on.job_ids() == {REFRESH_JOB_ID}

        off = Scheduler(db, timezone="Asia/Shanghai", submit=submit, refresh=refresh, refresh_interval=0)
        await off.sync()
        assert off.job_ids() == set()
    finally:
        await db.dispose()


async def test_run_refresh_calls_back(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        calls: list[int] = []

        async def refresh() -> None:
            calls.append(1)

        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit, refresh=refresh, refresh_interval=30)
        await scheduler.run_refresh()
        assert calls == [1]
    finally:
        await db.dispose()


async def test_run_refresh_swallows_failure(tmp_path: Path) -> None:
    """刷新会真的调模型和数据库，失败是常态；异常必须烂在这一层，否则调度循环会停。"""
    db = await _make_db(tmp_path)
    try:

        async def refresh() -> None:
            raise RuntimeError("模型挂了")

        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit, refresh=refresh, refresh_interval=30)
        await scheduler.run_refresh()  # 不抛就算过
    finally:
        await db.dispose()


async def test_run_refresh_without_callback_is_noop(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        _, submit = _collector()
        scheduler = Scheduler(db, timezone="Asia/Shanghai", submit=submit)
        await scheduler.run_refresh()
    finally:
        await db.dispose()
