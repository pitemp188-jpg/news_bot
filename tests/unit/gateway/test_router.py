"""
模块: tests.unit.gateway.test_router
职责: 校验入站鉴权与补投递、出站分段重试、waiting_user 状态与备用通道
依赖: gateway.router, gateway.auth
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter

from newsbot.core.config import DeliverySection
from newsbot.core.db import Database
from newsbot.core.models import Delivery, Report
from newsbot.gateway.auth import Authorizer
from newsbot.gateway.base import MessageEvent, SendResult
from newsbot.gateway.router import Router


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await db.init()
    return db


def _settings(**overrides: object) -> DeliverySection:
    base = {"max_attempts": 2, "fallback_platform": "qqbot", "chunk_delay_seconds": 0.0}
    base.update(overrides)
    return DeliverySection(**base)  # type: ignore[arg-type]


async def _instant(_seconds: float) -> None:
    return None


def _router(db: Database, *, inbound=None, auth: Authorizer | None = None, **overrides: object) -> Router:
    return Router(db, auth or Authorizer({"fake": {"*"}}), _settings(**overrides), inbound=inbound, sleeper=_instant)


async def test_start_and_stop_adapters(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        adapter = FakeAdapter()
        router.register(adapter)
        assert await router.start() == {"fake": True}
        assert router.online() == {"fake": True}
        await router.stop()
        assert adapter.disconnect_calls == 1
    finally:
        await db.dispose()


async def test_start_contains_adapter_failure(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        router.register(FakeAdapter(connect_ok=False))
        assert await router.start() == {"fake": False}
    finally:
        await db.dispose()


async def test_unauthorized_message_is_dropped(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    seen: list[MessageEvent] = []
    try:
        auth = Authorizer({"fake": {"u-ok"}})
        router = _router(db, auth=auth, inbound=_recorder(seen))
        adapter = FakeAdapter()
        router.register(adapter)

        await adapter.emit("你好", chat_id="u-bad", user_id="u-bad")
        assert seen == []
        pending = auth.pending()
        assert len(pending) == 1 and pending[0].user_id == "u-bad"

        await adapter.emit("你好", chat_id="u-ok", user_id="u-ok")
        assert [event.user_id for event in seen] == ["u-ok"]
    finally:
        await db.dispose()


async def test_authorized_message_flushes_waiting(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    seen: list[MessageEvent] = []
    try:
        router = _router(db, inbound=_recorder(seen))
        adapter = FakeAdapter()
        router.register(adapter)
        report_id = await _insert_report(db, "积压的日报")
        await _insert_delivery(db, report_id, status="waiting_user", chat_id="c1")

        await adapter.emit("在吗", chat_id="c1")
        assert adapter.sent == [("c1", "积压的日报", None)]
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "sent"
        assert len(seen) == 1
    finally:
        await db.dispose()


async def test_flush_keeps_waiting_when_still_blocked(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, inbound=_recorder([]))
        adapter = FakeAdapter(send_result=SendResult.failure("会话未就绪", need_user=True))
        router.register(adapter)
        report_id = await _insert_report(db, "积压内容")
        await _insert_delivery(db, report_id, status="waiting_user", chat_id="c1")

        assert await router.flush_waiting("fake", "c1") == 0
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "waiting_user"
        assert row.attempts == 1
    finally:
        await db.dispose()


async def test_flush_never_resends_sent_rows(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, inbound=_recorder([]))
        adapter = FakeAdapter()
        router.register(adapter)
        report_id = await _insert_report(db, "已送达")
        await _insert_delivery(db, report_id, status="sent", chat_id="c1")
        assert await router.flush_waiting("fake", "c1") == 0
        assert adapter.sent == []
    finally:
        await db.dispose()


async def test_missing_inbound_handler_is_safe(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        adapter = FakeAdapter()
        router.register(adapter)
        await adapter.emit("无人处理")  # 不应抛错
    finally:
        await db.dispose()


async def test_send_records_success(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        adapter = FakeAdapter()
        router.register(adapter)
        assert await router.send(platform="fake", chat_id="c1", text="内容", report_id=None) is True
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "sent" and row.sent_at is not None
    finally:
        await db.dispose()


async def test_send_marks_waiting_user(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        router.register(FakeAdapter(send_result=SendResult.failure("未就绪", need_user=True)))
        assert await router.send(platform="fake", chat_id="c1", text="内容") is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "waiting_user"
        assert row.error == "未就绪"
    finally:
        await db.dispose()


async def test_send_falls_back_to_other_platform(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, fallback_platform="qqbot")
        primary = FakeAdapter(platform="fake", send_result=SendResult.failure("坏了", retryable=False))
        backup = FakeAdapter(platform="qqbot")
        router.register(primary)
        router.register(backup)

        assert await router.send(platform="fake", chat_id="c1", text="内容") is True
        assert backup.sent == [("c1", "内容", None)]
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "sent"
    finally:
        await db.dispose()


async def test_send_retries_then_succeeds(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, max_attempts=3)
        adapter = FakeAdapter(send_result=SendResult.failure("超时", retryable=True))
        router.register(adapter)

        async def flaky(chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
            adapter.sent.append((chat_id, text, reply_to))
            return SendResult.success() if len(adapter.sent) >= 3 else SendResult.failure("超时", retryable=True)

        adapter.send = flaky  # type: ignore[method-assign]
        assert await router.send(platform="fake", chat_id="c1", text="内容") is True
        assert len(adapter.sent) == 3
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.attempts == 3
    finally:
        await db.dispose()


async def test_send_marks_failed_after_attempts(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, max_attempts=2, fallback_platform="")
        router.register(FakeAdapter(send_result=SendResult.failure("坏", retryable=True)))
        assert await router.send(platform="fake", chat_id="c1", text="内容") is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert row.status == "failed" and row.attempts == 2
    finally:
        await db.dispose()


async def test_send_with_unregistered_platform_fails(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, fallback_platform="")
        assert await router.send(platform="ghost", chat_id="c1", text="内容") is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert "未注册" in (row.error or "")
    finally:
        await db.dispose()


async def test_adapter_exception_is_contained(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db, max_attempts=1, fallback_platform="")
        adapter = FakeAdapter()

        async def boom(chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
            raise RuntimeError("适配器炸了")

        adapter.send = boom  # type: ignore[method-assign]
        router.register(adapter)
        assert await router.send(platform="fake", chat_id="c1", text="内容") is False
        async with db.session() as session:
            row = (await session.execute(select(Delivery))).scalar_one()
        assert "RuntimeError" in (row.error or "")
    finally:
        await db.dispose()


async def test_router_exposes_platforms(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        router = _router(db)
        router.register(FakeAdapter(platform="weixin"))
        assert router.platforms == ["weixin"]
        assert router.get("weixin") is not None
        assert router.get("nope") is None
    finally:
        await db.dispose()


def _recorder(sink: list[MessageEvent]):
    async def handler(event: MessageEvent) -> None:
        sink.append(event)

    return handler


async def _insert_report(db: Database, content: str) -> int:
    async with db.session() as session:
        report = Report(content=content, sources=[])
        session.add(report)
        await session.commit()
        await session.refresh(report)
        return report.id


async def _insert_delivery(
    db: Database,
    report_id: int | None,
    *,
    status: str,
    chat_id: str,
    platform: str = "fake",
    content: str | None = None,
) -> None:
    async with db.session() as session:
        session.add(Delivery(report_id=report_id, platform=platform, chat_id=chat_id, status=status, content=content))
        await session.commit()
