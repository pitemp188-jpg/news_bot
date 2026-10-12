"""
模块: tests.unit.gateway.test_qqbot
职责: 校验 QQ 适配器——token 刷新、WebSocket 协议处理、入站解析、发送与重连
依赖: gateway.qqbot
"""

from __future__ import annotations

import asyncio
import json
import time
import types

import httpx
import pytest
import respx
from tests.fakes.qq_gateway import FakeWS, c2c_message, control, group_message, hello, ready
from websockets.exceptions import ConnectionClosed

from newsbot.core.config import Secrets
from newsbot.gateway import qqbot as qq_module
from newsbot.gateway.base import MessageEvent, SendResult
from newsbot.gateway.qqbot import GATEWAY_PATH, QQBotAdapter, QQCloseError, _parse_timestamp, _strip_mention

TOKEN_URL = qq_module.TOKEN_URL
GATEWAY_URL = f"{qq_module.API_BASE}{qq_module.GATEWAY_PATH}"


def _secrets() -> Secrets:
    return Secrets(qq_app_id="123456", qq_client_secret="secret-value")


def _token_response(expires_in: int = 7200, token: str = "tk-1") -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})


def _adapter(ws: FakeWS | None = None) -> QQBotAdapter:
    adapter = QQBotAdapter(_secrets(), connect_ws=_connector(ws))
    return adapter


def _connector(ws: FakeWS | None):
    async def connect(_url: str) -> FakeWS:
        return ws if ws is not None else FakeWS()

    return connect


def _collect(adapter: QQBotAdapter) -> list[MessageEvent]:
    events: list[MessageEvent] = []

    async def handler(event: MessageEvent) -> None:
        events.append(event)

    adapter.set_handler(handler)
    return events


def _preauth(adapter: QQBotAdapter) -> None:
    """预置有效 token，避免测试触发真实网络。"""
    adapter._token = "tk-test"
    adapter._token_expires_at = time.time() + 3600


async def _settle(times: int = 6) -> None:
    """让出若干轮事件循环，使连接、读取与 Identify 等后台任务完成。"""
    for _ in range(times):
        await asyncio.sleep(0)


@respx.mock
async def test_connect_requires_credentials() -> None:
    adapter = QQBotAdapter(Secrets(qq_app_id="", qq_client_secret=""))
    assert await adapter.connect() is False
    assert adapter.is_connected is False


@respx.mock
async def test_connect_missing_gateway_url_fails() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(GATEWAY_URL).mock(return_value=httpx.Response(200, json={}))
    adapter = _adapter()
    assert await adapter.connect() is False


@respx.mock
async def test_connect_and_disconnect() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(GATEWAY_URL).mock(return_value=httpx.Response(200, json={"url": "wss://gw.example/ws"}))
    ws = FakeWS([hello()])
    adapter = _adapter(ws)

    assert await adapter.connect() is True
    assert adapter.is_connected is True
    await _settle()
    identify = next(frame for frame in ws.sent if frame["op"] == qq_module.OP_IDENTIFY)
    assert identify["d"]["intents"] == qq_module.INTENTS
    assert identify["d"]["token"].startswith("QQBot ")

    await adapter.disconnect()
    assert adapter.is_connected is False
    assert ws.closed is True


@respx.mock
async def test_token_is_cached_across_calls() -> None:
    token_route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(GATEWAY_URL).mock(return_value=httpx.Response(200, json={"url": "wss://gw.example/ws"}))
    adapter = _adapter(FakeWS([hello()]))
    await adapter.connect()
    try:
        await adapter._ensure_token()
        await adapter._api("GET", GATEWAY_PATH)
        assert token_route.call_count == 1
    finally:
        await adapter.disconnect()


@respx.mock
async def test_expired_token_is_refreshed() -> None:
    token_route = respx.post(TOKEN_URL).mock(return_value=_token_response(expires_in=0, token="tk-2"))
    respx.get(GATEWAY_URL).mock(return_value=httpx.Response(200, json={"url": "wss://gw.example/ws"}))
    adapter = _adapter(FakeWS([hello()]))
    await adapter.connect()
    try:
        await adapter._ensure_token()
        assert token_route.call_count >= 2
    finally:
        await adapter.disconnect()


async def test_send_without_connection_is_retryable() -> None:
    adapter = _adapter()
    result = await adapter.send("u1", "内容")
    assert result.ok is False and result.retryable is True


async def test_send_empty_text_is_noop() -> None:
    adapter = _adapter()
    adapter._running = True
    assert (await adapter.send("u1", "   ")).ok is True


@respx.mock
async def test_send_uses_c2c_path_and_returns_id() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    message_route = respx.post(f"{qq_module.API_BASE}/v2/users/u1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-9"})
    )
    adapter = _adapter()
    adapter._running = True
    try:
        result = await adapter.send("u1", "今天有三条新闻")
    finally:
        await adapter.disconnect()
    assert result.ok is True and result.message_id == "msg-9"
    body = json.loads(message_route.calls[0].request.content)
    assert body["msg_type"] == qq_module.MSG_TYPE_TEXT
    assert 0 <= body["msg_seq"] <= 65535


@respx.mock
async def test_send_to_group_uses_group_path_and_reply_id() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.post(f"{qq_module.API_BASE}/v2/groups/g1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg-10"})
    )
    adapter = _adapter()
    adapter._running = True
    adapter._chat_types["g1"] = "group"
    try:
        result = await adapter.send("g1", "群消息", reply_to="m2")
    finally:
        await adapter.disconnect()
    assert result.ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["msg_id"] == "m2"


@respx.mock
async def test_send_splits_long_text() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.post(f"{qq_module.API_BASE}/v2/users/u1/messages").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    adapter = _adapter()
    adapter._running = True
    try:
        await adapter.send("u1", "长文本。" * 800)
    finally:
        await adapter.disconnect()
    assert route.call_count > 1


@respx.mock
async def test_send_error_is_classified() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.post(f"{qq_module.API_BASE}/v2/users/u1/messages").mock(
        return_value=httpx.Response(500, json={"message": "timeout"})
    )
    adapter = _adapter()
    adapter._running = True
    try:
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is False and result.retryable is True


@respx.mock
async def test_send_forbidden_marks_need_user() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.post(f"{qq_module.API_BASE}/v2/users/u1/messages").mock(
        return_value=httpx.Response(403, json={"message": "no permission"})
    )
    adapter = _adapter()
    adapter._running = True
    try:
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is False and result.need_user is True


async def test_hello_sends_identify_async() -> None:
    ws = FakeWS()
    adapter = _adapter(ws)
    adapter._ws = ws
    _preauth(adapter)
    adapter._handle_payload(hello(20000))
    await _settle()
    identify = next(frame for frame in ws.sent if frame["op"] == qq_module.OP_IDENTIFY)
    assert identify["d"]["intents"] == qq_module.INTENTS
    assert identify["d"]["token"].startswith("QQBot ")


async def test_hello_resumes_when_session_exists() -> None:
    ws = FakeWS()
    adapter = _adapter(ws)
    adapter._ws = ws
    adapter._session_id = "sess-9"
    adapter._last_seq = 42
    _preauth(adapter)
    adapter._handle_payload(hello())
    await _settle()
    resume = next(frame for frame in ws.sent if frame["op"] == qq_module.OP_RESUME)
    assert resume["d"] == {"token": resume["d"]["token"], "session_id": "sess-9", "seq": 42}


async def test_ready_stores_session_and_seq() -> None:
    adapter = _adapter()
    adapter._handle_payload(ready("sess-77"))
    assert adapter._session_id == "sess-77"
    assert adapter._last_seq == 1


async def test_c2c_message_is_dispatched() -> None:
    adapter = _adapter()
    events = _collect(adapter)
    adapter._handle_payload(c2c_message("查一下新闻"))
    await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0].chat_type == "dm"
    assert events[0].chat_id == "u1"
    assert events[0].text == "查一下新闻"
    assert adapter._chat_types["u1"] == "c2c"


async def test_group_message_strips_mention() -> None:
    adapter = _adapter()
    events = _collect(adapter)
    adapter._handle_payload(group_message("@机器人 今天有什么新闻"))
    await asyncio.sleep(0)
    assert events[0].chat_type == "group"
    assert events[0].chat_id == "g1"
    assert events[0].user_id == "u2"
    assert events[0].text == "今天有什么新闻"


async def test_group_message_without_mention_is_dropped() -> None:
    adapter = _adapter()
    events = _collect(adapter)
    adapter._handle_payload(group_message("@机器人"))
    await asyncio.sleep(0)
    assert events == []


async def test_invalid_session_clears_state() -> None:
    ws = FakeWS()
    adapter = _adapter(ws)
    adapter._ws = ws
    adapter._session_id = "sess"
    adapter._last_seq = 5
    adapter._handle_payload(control(qq_module.OP_INVALID_SESSION, False))
    await asyncio.sleep(0)
    assert adapter._session_id is None and adapter._last_seq is None
    assert ws.closed is True


async def test_server_reconnect_closes_ws() -> None:
    ws = FakeWS()
    adapter = _adapter(ws)
    adapter._ws = ws
    adapter._handle_payload(control(qq_module.OP_RECONNECT))
    await asyncio.sleep(0)
    assert ws.closed is True


async def test_broken_frame_is_ignored() -> None:
    adapter = _adapter()
    adapter._handle_payload("{不是 JSON")
    adapter._handle_payload(json.dumps([1, 2, 3]))
    assert adapter._last_seq is None


async def test_read_events_raises_on_close() -> None:
    adapter = _adapter()
    ws = FakeWS()
    adapter._ws = ws
    adapter._running = True
    with pytest.raises(QQCloseError):
        await adapter._read_events()


async def test_fatal_close_code_stops_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(FakeWS())
    adapter._running = True

    async def boom() -> None:
        raise QQCloseError(4001, "invalid opcode")

    monkeypatch.setattr(adapter, "_read_events", boom)
    await adapter._listen_loop()
    assert adapter._running is False


async def test_quick_disconnects_stop_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(FakeWS())
    adapter._running = True

    async def instant(_seconds: float = 0.0) -> None:
        return None

    async def boom() -> None:
        raise QQCloseError(1000, "")

    monkeypatch.setattr(adapter, "_read_events", boom)
    monkeypatch.setattr(qq_module.asyncio, "sleep", instant)
    monkeypatch.setattr(qq_module, "QUICK_DISCONNECT_SECONDS", 999.0)
    await adapter._listen_loop()
    assert adapter._running is False


async def test_rate_limited_close_waits_then_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(FakeWS())
    adapter._running = True
    calls: list[str] = []

    async def sequence() -> None:
        calls.append("read")
        if len(calls) == 1:
            raise QQCloseError(4008, "rate limit")
        adapter._running = False

    async def instant(_seconds: float = 0.0) -> None:
        return None

    async def reopen() -> None:
        calls.append("reopen")

    monkeypatch.setattr(adapter, "_read_events", sequence)
    monkeypatch.setattr(adapter, "_open_ws", reopen)
    monkeypatch.setattr(qq_module.asyncio, "sleep", instant)
    await adapter._listen_loop()
    assert "reopen" in calls


async def test_healthy_connection_resets_reconnect_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接实打实用过一段时间后断开，重连计数必须清零（回归用例）。

    实测缺陷：计数只在 `_read_events()` 正常返回时才清零，而断开走的是异常路径，
    于是计数跨"多次健康连接"累加——挂机一整天（休眠唤醒、网关滚动重启）攒够
    上限就永久离线，进程却还活着。
    """
    adapter = _adapter(FakeWS())
    adapter._running = True
    seen_backoff: list[int] = []
    calls: list[int] = []

    async def sequence() -> None:
        calls.append(1)
        if len(calls) <= 3:
            raise QQCloseError(1000, "")
        adapter._running = False
        return

    async def record_sleep(_seconds: float = 0.0) -> None:
        seen_backoff.append(len(calls))
        return

    async def reopen() -> None:
        return

    monkeypatch.setattr(adapter, "_read_events", sequence)
    monkeypatch.setattr(adapter, "_open_ws", reopen)
    monkeypatch.setattr(qq_module.asyncio, "sleep", record_sleep)
    # 连接时长视为"非瞬时"，即每次都是健康连接后断开
    monkeypatch.setattr(qq_module, "QUICK_DISCONNECT_SECONDS", 0.0)
    await adapter._listen_loop()

    assert adapter._running is False
    assert len(calls) == 4, "每次断开都要重连，不能因计数累加而放弃"


async def test_reconnect_never_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续大量失败也不停止重连：进程要挂机跑一整天，必须能自愈。

    原来的实现超过 5 次就 `self._running = False`，机器人从此永久离线，
    而进程还在——只看进程存活的监控发现不了。真正该停机的是"反复瞬间断开"
    与不可恢复关闭码。
    """
    adapter = _adapter(FakeWS())
    adapter._running = True
    attempts = 0
    opened = 0
    delays: list[float] = []

    async def sequence() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 40:
            raise QQCloseError(1000, "")
        adapter._running = False

    async def instant(seconds: float = 0.0) -> None:
        delays.append(seconds)
        return None

    async def reopen() -> None:
        nonlocal opened
        opened += 1

    monkeypatch.setattr(adapter, "_read_events", sequence)
    monkeypatch.setattr(adapter, "_open_ws", reopen)
    monkeypatch.setattr(qq_module.asyncio, "sleep", instant)
    # 把"瞬时断开"阈值设为负值，确保不走快速断开那条停机分支
    monkeypatch.setattr(qq_module, "QUICK_DISCONNECT_SECONDS", -1.0)
    await adapter._listen_loop()
    assert attempts >= 40, f"不应在 {attempts} 次后放弃"
    # 每次循环都必须真的重连一次、且睡眠一次：退避与重连若被写到 while 外，
    # 就变成零延迟疯狂重连（会撞限流），而且 _running=False 后还会多连一次。
    assert opened == attempts, f"重连次数 {opened} != 断开次数 {attempts}"
    assert len(delays) == attempts, f"睡眠次数 {len(delays)} != {attempts}"
    assert delays[0] == qq_module.RECONNECT_BACKOFF[0], "首次退避必须走最短间隔"
    assert max(delays) <= qq_module.RECONNECT_BACKOFF[-1], "退避不得超上限"


async def test_token_invalid_close_refreshes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(FakeWS())
    adapter._running = True
    adapter._token = "old"
    adapter._token_expires_at = 9_999_999_999.0
    calls: list[str] = []

    async def sequence() -> None:
        calls.append("read")
        if len(calls) == 1:
            raise QQCloseError(4004, "invalid token")
        adapter._running = False

    async def instant(_seconds: float = 0.0) -> None:
        return None

    async def reopen() -> None:
        return

    monkeypatch.setattr(adapter, "_read_events", sequence)
    monkeypatch.setattr(adapter, "_open_ws", reopen)
    monkeypatch.setattr(qq_module.asyncio, "sleep", instant)
    await adapter._listen_loop()
    assert adapter._token is None


async def test_check_payload_ignores_heartbeat_ack() -> None:
    adapter = _adapter()
    adapter._last_seq = 3
    adapter._handle_payload(control(qq_module.OP_HEARTBEAT_ACK))
    assert adapter._last_seq == 3


@pytest.mark.parametrize(
    ("raw", "expected_year"),
    [("2026-10-09T12:00:00+08:00", 2026), (1_760_000_000_000, 2025)],
)
def test_timestamp_parsing(raw: object, expected_year: int) -> None:
    assert _parse_timestamp(raw).year == expected_year


def test_timestamp_falls_back_to_now() -> None:
    assert _parse_timestamp(None).year >= 2026


@pytest.mark.parametrize(
    ("text", "expected"),
    [("@机器人 查新闻", "查新闻"), ("@机器人", ""), ("没有提及", "没有提及")],
)
def test_strip_mention(text: str, expected: str) -> None:
    assert _strip_mention(text) == expected


async def test_dispatch_requires_handler() -> None:
    adapter = _adapter()
    adapter._handle_payload(c2c_message())
    await asyncio.sleep(0)  # 未注册回调时只记录日志
    assert adapter.is_connected is False


async def test_send_result_helper_shape() -> None:
    assert SendResult.success().ok is True


def _closed(code: int) -> Exception:
    """造一个 websockets 风格的关闭异常（16.x 里关闭码在 rcvd/sent 上）。"""
    exc = ConnectionClosed(None, None)
    exc.rcvd = types.SimpleNamespace(code=code, reason="Session timed out")
    return exc


async def test_library_close_is_translated_with_its_code() -> None:
    """websockets 抛的关闭必须带上真实关闭码。

    实测缺陷：库抛的关闭走的是通用 `except Exception` 分支，只记一句日志，
    关闭码**全部**丢失——4004（刷新 token）、4009（清会话）、4013（不可恢复）
    这些处理分支根本没机会执行。实测机器人因此离线 33 小时。
    """
    adapter = _adapter()
    adapter._ws = FakeWS([_closed(4009)])
    adapter._running = True
    with pytest.raises(QQCloseError) as info:
        await adapter._read_events()
    assert info.value.code == 4009


def test_session_timeout_code_invalidates_session() -> None:
    """4009 必须归入会话失效集合，否则会一直 Resume 一个已作废的 session。"""
    assert 4009 in qq_module.SESSION_INVALID_CLOSE_CODES


class _HangingWS:
    """永不吐帧的连接：模拟半死链路（TCP 还在，对端不再回话）。"""

    async def recv(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("不会执行到这里")

    async def close(self) -> None:
        return None


async def test_session_timeout_clears_session_so_next_hello_identifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收到 4009 后要清掉 session，下一次 HELLO 走 Identify 而不是 Resume。

    这是实测的永久离线路径：session 被服务端丢弃后仍带着它 Resume，服务端既不回
    RESUMED 也不报错，只能等下一次 4009（每次一小时），于是无限循环。
    """
    adapter = _adapter()
    ws = FakeWS()
    adapter._ws = ws
    adapter._session_id = "stale"
    adapter._last_seq = 9
    adapter._running = True
    calls = 0

    async def sequence() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QQCloseError(4009, "Session timed out")
        adapter._running = False

    async def instant(_seconds: float = 0.0) -> None:
        return None

    async def reopen() -> None:
        return None

    monkeypatch.setattr(adapter, "_read_events", sequence)
    monkeypatch.setattr(adapter, "_open_ws", reopen)
    monkeypatch.setattr(qq_module.asyncio, "sleep", instant)
    await adapter._listen_loop()

    assert adapter._session_id is None, "4009 后必须清掉 session"
    assert adapter._last_seq is None

    # 先撤销 sleep 替换：`qq_module.asyncio` 就是全局 asyncio，把它换成 instant 之后
    # _settle() 里的 sleep(0) 不再让出事件循环，_spawn 出去的任务永远不会执行。
    monkeypatch.undo()
    # 再收到 HELLO 时必须重新 Identify（而不是用作废的 session 去 Resume）
    _preauth(adapter)
    adapter._handle_payload(hello())
    await _settle()
    kinds = [frame["op"] for frame in ws.sent]
    assert qq_module.OP_IDENTIFY in kinds, f"应重新 Identify，实际发送 {kinds}"
    assert qq_module.OP_RESUME not in kinds, "session 已作废，不该再 Resume"


async def test_silent_link_is_dropped_after_frame_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """长时间收不到任何帧要主动断开，不能静默挂着。

    实测链路半死时 recv() 会一直阻塞，一次重连周期长达一小时，这期间所有消息都
    收不到，日志里也没有任何异常。
    """
    adapter = _adapter()
    adapter._ws = _HangingWS()
    adapter._running = True
    # 存活上限有 30s 下限（避免把正常链路误判为已死，由 test_frame_timeout_has_a_floor
    # 覆盖），要让本测试秒级完成就得连下限一起改小
    monkeypatch.setattr(qq_module, "MIN_FRAME_TIMEOUT", 0.02)
    monkeypatch.setattr(adapter, "_heartbeat_interval", 0.01)
    # 外面再套一层 wait_for：缺了存活上限时这里必须**失败**而不是把测试永久挂住，
    # 否则 CI 会一直卡着不返回（实测会挂死）
    with pytest.raises(QQCloseError) as info:
        await asyncio.wait_for(adapter._read_events(), timeout=5.0)
    assert "静默" in info.value.reason


def test_frame_timeout_has_a_floor() -> None:
    """心跳周期很小时也要留出下限，否则会把正常链路误判为已死。"""
    adapter = _adapter()
    adapter._heartbeat_interval = 0.001
    assert adapter._frame_timeout >= qq_module.MIN_FRAME_TIMEOUT


def test_close_code_falls_back_to_sent_frame() -> None:
    """只收到对方关闭时 rcvd 为空，要退到 sent 的关闭码。"""
    exc = ConnectionClosed(None, None)
    exc.rcvd = None
    exc.sent = types.SimpleNamespace(code=1000, reason="ok")
    assert qq_module._close_code(exc) == 1000
    assert qq_module._close_code(RuntimeError("普通异常")) is None
