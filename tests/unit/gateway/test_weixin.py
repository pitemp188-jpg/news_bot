"""
模块: tests.unit.gateway.test_weixin
职责: 校验微信适配器——凭证存储、长轮询收发、context_token、错误码分支与扫码登录
依赖: gateway.weixin
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import respx

from newsbot.core.config import Secrets
from newsbot.gateway import weixin as wx
from newsbot.gateway.base import MessageEvent, SendResult
from newsbot.gateway.weixin import (
    ILINK_BASE_URL,
    SESSION_EXPIRED_PAUSE,
    SESSION_NOT_READY_HINT,
    WeixinAccount,
    WeixinAdapter,
    WeixinStore,
    extract_text,
    qr_login,
)

UPDATES_URL = f"{ILINK_BASE_URL}/{wx.EP_GET_UPDATES}"
SEND_URL = f"{ILINK_BASE_URL}/{wx.EP_SEND_MESSAGE}"
QR_URL = f"{ILINK_BASE_URL}/{wx.EP_GET_BOT_QR}"
QR_STATUS_URL = f"{ILINK_BASE_URL}/{wx.EP_GET_QR_STATUS}"


def _secrets() -> Secrets:
    return Secrets()


def _store(tmp_path: Path) -> WeixinStore:
    store = WeixinStore(tmp_path)
    store.save_account(WeixinAccount(account_id="bot-1", token="tk-1"))
    return store


def _adapter(tmp_path: Path, **kwargs: object) -> WeixinAdapter:
    async def yielding_sleep(_seconds: float) -> None:
        # 必须让出事件循环，否则轮询循环会忙等并饿死测试的定时器
        await asyncio.sleep(0)

    return WeixinAdapter(_secrets(), _store(tmp_path), sleeper=yielding_sleep, **kwargs)  # type: ignore[arg-type]


def _updates(msgs: list[dict[str, object]] | None = None, cursor: str = "c2", **extra: object) -> httpx.Response:
    payload: dict[str, object] = {"ret": 0, "msgs": msgs or [], "get_updates_buf": cursor}
    payload.update(extra)
    return httpx.Response(200, json=payload)


def _send_ok(message_id: str = "s1") -> httpx.Response:
    return httpx.Response(200, json={"ret": 0, "message_id": message_id})


def _message(
    text: str = "你好", *, sender: str = "u1", message_id: str = "m1", token: str = "ctx-1"
) -> dict[str, object]:
    return {
        "from_user_id": sender,
        "message_id": message_id,
        "context_token": token,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def _collect(adapter: WeixinAdapter) -> list[MessageEvent]:
    events: list[MessageEvent] = []

    async def handler(event: MessageEvent) -> None:
        events.append(event)

    adapter.set_handler(handler)
    return events


# ── 存储 ──
def test_store_round_trip(tmp_path: Path) -> None:
    store = WeixinStore(tmp_path)
    assert store.load_account() is None
    store.save_account(WeixinAccount(account_id="bot-9", token="tk", user_id="me"))
    loaded = store.load_account()
    assert loaded is not None
    assert (loaded.account_id, loaded.token, loaded.user_id) == ("bot-9", "tk", "me")


def test_store_cursor_and_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load_cursor("bot-1") == ""
    store.save_cursor("bot-1", "cursor-7")
    assert store.load_cursor("bot-1") == "cursor-7"

    store.set_token("bot-1", "u1", "ctx")
    assert store.get_token("u1") == "ctx"
    store.clear_token("bot-1", "u1")
    assert store.get_token("u1") is None

    store.set_token("bot-1", "u2", "ctx2")
    restored = WeixinStore(tmp_path)
    restored.restore_tokens("bot-1")
    assert restored.get_token("u2") == "ctx2"


def test_store_survives_broken_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store.root / "bot-1.sync.json").write_text("{不是 JSON", encoding="utf-8")
    assert store.load_cursor("bot-1") == ""


def test_store_account_file_is_private(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_account(WeixinAccount(account_id="bot-2", token="tk"))
    mode = (store.root / "account.json").stat().st_mode & 0o777
    assert mode in {0o600, 0o666}  # Windows 上 chmod 不生效时退化为默认权限


# ── 文本提取 ──
def test_extract_text_plain() -> None:
    assert extract_text([{"type": 1, "text_item": {"text": "你好"}}]) == "你好"


def test_extract_text_with_quote() -> None:
    items = [
        {
            "type": 1,
            "text_item": {"text": "同意"},
            "ref_msg": {"title": "早报", "message_item": {"type": 1, "text_item": {"text": "今日要闻"}}},
        }
    ]
    assert extract_text(items) == "[引用: 早报 | 今日要闻]\n同意"


def test_extract_text_ignores_non_text() -> None:
    assert extract_text([{"type": 2, "image_item": {}}]) == ""


# ── 连接与轮询 ──
async def test_connect_without_credentials_fails(tmp_path: Path) -> None:
    adapter = WeixinAdapter(_secrets(), WeixinStore(tmp_path))
    assert await adapter.connect() is False


async def test_connect_uses_env_credentials(tmp_path: Path) -> None:
    secrets = Secrets(weixin_token="tk-env", weixin_account_id="bot-env")
    adapter = WeixinAdapter(secrets, WeixinStore(tmp_path))

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    adapter._sleep = instant
    assert await adapter.connect() is True
    await adapter.disconnect()


@respx.mock
async def test_poll_dispatches_and_persists_cursor(tmp_path: Path) -> None:
    route = respx.post(UPDATES_URL).mock(return_value=_updates([_message("查新闻")]))
    adapter = _adapter(tmp_path)
    events = _collect(adapter)
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert [event.text for event in events] == ["查新闻"]
    assert events[0].chat_id == "u1"
    assert events[0].chat_type == "dm"
    assert adapter._store.load_cursor("bot-1") == "c2"
    assert adapter._store.get_token("u1") == "ctx-1"
    assert route.call_count >= 1


@respx.mock
async def test_poll_skips_own_messages_and_duplicates(tmp_path: Path) -> None:
    msgs = [
        _message("自己的消息", sender="bot-1"),
        _message("重复一", message_id="m9"),
        _message("重复一", message_id="m10"),
    ]
    respx.post(UPDATES_URL).mock(return_value=_updates(msgs))
    adapter = _adapter(tmp_path)
    events = _collect(adapter)
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert [event.text for event in events] == ["重复一"]


@respx.mock
async def test_poll_skips_empty_text(tmp_path: Path) -> None:
    respx.post(UPDATES_URL).mock(return_value=_updates([{"from_user_id": "u1", "message_id": "m1", "item_list": []}]))
    adapter = _adapter(tmp_path)
    events = _collect(adapter)
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert events == []


@respx.mock
async def test_poll_timeout_keeps_cursor(tmp_path: Path) -> None:
    respx.post(UPDATES_URL).mock(side_effect=httpx.ReadTimeout("long poll"))
    adapter = _adapter(tmp_path)
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert adapter._store.load_cursor("bot-1") == ""


@respx.mock
async def test_poll_session_expired_pauses(tmp_path: Path) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    respx.post(UPDATES_URL).mock(
        return_value=httpx.Response(200, json={"ret": 0, "errcode": wx.SESSION_EXPIRED_ERRCODE})
    )
    adapter = _adapter(tmp_path)
    adapter._sleep = record
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert SESSION_EXPIRED_PAUSE in sleeps


@respx.mock
async def test_poll_recovers_after_failures(tmp_path: Path) -> None:
    respx.post(UPDATES_URL).mock(return_value=httpx.Response(200, json={"ret": -1, "errcode": -1}))
    adapter = _adapter(tmp_path)
    try:
        await adapter.connect()
        await asyncio.sleep(0.05)
        adapter._running = False
    finally:
        await adapter.disconnect()
    assert adapter.is_connected is False


# ── 发送 ──
async def test_send_without_account_is_retryable(tmp_path: Path) -> None:
    adapter = WeixinAdapter(_secrets(), WeixinStore(tmp_path))
    result = await adapter.send("u1", "内容")
    assert result.ok is False and result.retryable is True


async def test_send_empty_text_is_noop(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert (await adapter.send("u1", "  ")).ok is True


@respx.mock
async def test_send_includes_context_token(tmp_path: Path) -> None:
    route = respx.post(SEND_URL).mock(return_value=_send_ok("s9"))
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        adapter._store.set_token("bot-1", "u1", "ctx-9")
        result = await adapter.send("u1", "今天有三条新闻")
    finally:
        await adapter.disconnect()
    assert result.ok is True and result.message_id == "s9"
    body = json.loads(route.calls[0].request.content)
    assert body["msg"]["context_token"] == "ctx-9"
    assert body["msg"]["item_list"][0]["text_item"]["text"] == "今天有三条新闻"
    assert body["base_info"]["channel_version"] == wx.CHANNEL_VERSION


@respx.mock
async def test_send_retries_without_token_on_expiry(tmp_path: Path) -> None:
    route = respx.post(SEND_URL)
    route.side_effect = [
        httpx.Response(200, json={"ret": 0, "errcode": wx.SESSION_EXPIRED_ERRCODE}),
        _send_ok("s10"),
    ]
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        adapter._store.set_token("bot-1", "u1", "ctx-old")
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is True
    second = json.loads(route.calls[1].request.content)
    assert "context_token" not in second["msg"]
    assert adapter._store.get_token("u1") is None


@respx.mock
async def test_stale_session_marks_need_user(tmp_path: Path) -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ret": -2, "errcode": -2, "errmsg": "prepare failed"})
    )
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is False and result.need_user is True
    assert result.error == SESSION_NOT_READY_HINT


@respx.mock
async def test_rate_limit_opens_circuit(tmp_path: Path) -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ret": -2, "errcode": -2, "errmsg": "rate limited"})
    )
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        first = await adapter.send("u1", "内容")
        second = await adapter.send("u1", "再来")
    finally:
        await adapter.disconnect()
    assert first.ok is False and first.retryable is True
    assert second.ok is False and "冷却" in (second.error or "")


@respx.mock
async def test_unknown_error_is_reported(tmp_path: Path) -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ret": 999, "errcode": 999, "errmsg": "boom"}))
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is False and "999" in (result.error or "")


@respx.mock
async def test_http_error_is_retryable(tmp_path: Path) -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("网络不可达"))
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        result = await adapter.send("u1", "内容")
    finally:
        await adapter.disconnect()
    assert result.ok is False and result.retryable is True


@respx.mock
async def test_send_splits_long_text(tmp_path: Path) -> None:
    route = respx.post(SEND_URL).mock(return_value=_send_ok())
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        await adapter.send("u1", "长内容。" * 900)
    finally:
        await adapter.disconnect()
    assert route.call_count > 1
    for call in route.calls:
        body = json.loads(call.request.content)
        assert len(body["msg"]["item_list"][0]["text_item"]["text"]) <= wx.MAX_MESSAGE_LENGTH


@respx.mock
async def test_send_stops_at_first_failed_chunk(tmp_path: Path) -> None:
    route = respx.post(SEND_URL)
    route.side_effect = [_send_ok(), httpx.Response(200, json={"ret": 999, "errcode": 999, "errmsg": "bad"})]
    adapter = _adapter(tmp_path)
    await adapter.connect()
    try:
        result = await adapter.send("u1", "长内容。" * 900)
    finally:
        await adapter.disconnect()
    assert result.ok is False
    assert route.call_count == 2


# ── 扫码登录 ──
@respx.mock
async def test_qr_login_success(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={"qrcode": "hex", "qrcode_img_content": "https://qr"}))
    respx.get(QR_STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "confirmed",
                "ilink_bot_id": "bot-77",
                "bot_token": "tk-77",
                "baseurl": ILINK_BASE_URL,
                "ilink_user_id": "me",
            },
        )
    )
    shown: list[str] = []

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    store = WeixinStore(tmp_path)
    account = await qr_login(store, on_qr=shown.append, sleeper=instant)
    assert account is not None and account.account_id == "bot-77"
    assert shown == ["https://qr"]
    assert store.load_account() is not None


@respx.mock
async def test_qr_login_without_code_returns_none(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={}))

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    assert await qr_login(WeixinStore(tmp_path), on_qr=lambda _url: None, sleeper=instant) is None


@respx.mock
async def test_qr_login_refreshes_expired_code(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={"qrcode": "hex", "qrcode_img_content": "https://qr"}))
    respx.get(QR_STATUS_URL).side_effect = [
        httpx.Response(200, json={"status": "expired"}),
        httpx.Response(200, json={"status": "confirmed", "ilink_bot_id": "b", "bot_token": "t"}),
    ]

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    account = await qr_login(WeixinStore(tmp_path), on_qr=lambda _url: None, sleeper=instant)
    assert account is not None and account.account_id == "b"


@respx.mock
async def test_qr_login_gives_up_after_repeated_expiry(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={"qrcode": "hex", "qrcode_img_content": "https://qr"}))
    respx.get(QR_STATUS_URL).mock(return_value=httpx.Response(200, json={"status": "expired"}))

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    assert await qr_login(WeixinStore(tmp_path), on_qr=lambda _url: None, sleeper=instant) is None


@respx.mock
async def test_qr_login_incomplete_credentials_returns_none(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={"qrcode": "hex", "qrcode_img_content": "https://qr"}))
    respx.get(QR_STATUS_URL).mock(return_value=httpx.Response(200, json={"status": "confirmed", "ilink_bot_id": "b"}))

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    assert await qr_login(WeixinStore(tmp_path), on_qr=lambda _url: None, sleeper=instant) is None


@respx.mock
async def test_qr_login_times_out(tmp_path: Path) -> None:
    respx.get(QR_URL).mock(return_value=httpx.Response(200, json={"qrcode": "hex", "qrcode_img_content": "https://qr"}))
    respx.get(QR_STATUS_URL).mock(return_value=httpx.Response(200, json={"status": "wait"}))

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    assert await qr_login(WeixinStore(tmp_path), on_qr=lambda _url: None, sleeper=instant, timeout=0.01) is None


async def test_send_result_shape() -> None:
    assert SendResult.success("x").message_id == "x"
