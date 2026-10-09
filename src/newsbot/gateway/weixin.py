"""
模块: gateway.weixin
职责: 微信适配器——iLink Bot 接口长轮询收消息、按 context_token 回复、扫码登录与本地凭证
依赖: gateway.base, core.config, core.errors, core.log
来源: 精简自 hermes-agent@908e4a4 gateway/platforms/weixin.py（MIT），
      删除媒体 AES 上传下载、输入状态、群策略、平台锁与 markdown 换行美化
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from newsbot.core.config import Secrets
from newsbot.core.log import get_logger
from newsbot.gateway.base import BaseAdapter, MessageDeduplicator, MessageEvent, SendResult, safe_id

logger = get_logger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
APP_CLIENT_VERSION = (2 << 16) | (2 << 8)
LONG_POLL_TIMEOUT = 35.0
API_TIMEOUT = 15.0
QR_TIMEOUT = 35.0

MAX_MESSAGE_LENGTH = 2000
CHUNK_DELAY = 1.5
CHUNK_RETRIES = 3
RETRY_DELAY = 1.0
RATE_LIMIT_COOLDOWN = 30.0
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_BACKOFF = 30.0
SESSION_EXPIRED_ERRCODE = -14
STALE_SESSION_ERRCODE = -2
STALE_SESSION_MESSAGES = {"unknown error", "prepare failed"}
SESSION_EXPIRED_PAUSE = 600.0
# iLink 在约 2048 字处切分，留出余量
SPLIT_THRESHOLD = 1800

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

DEDUP_TTL = 300.0

# 会话过期 / 会话未就绪的提示，最终都会转成 need_user，等用户下次发消息再补投
SESSION_NOT_READY_HINT = "iLink 会话未就绪：需要用户先给机器人发一条消息"


def _headers(token: str | None) -> dict[str, str]:
    uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode("utf-8")).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_stale_session(ret: Any, errcode: Any, errmsg: Any) -> bool:
    code = ret if ret is not None else errcode
    if code != STALE_SESSION_ERRCODE:
        return False
    return str(errmsg or "").strip().lower() in STALE_SESSION_MESSAGES


def _is_session_expired(ret: Any, errcode: Any) -> bool:
    return SESSION_EXPIRED_ERRCODE in (ret, errcode)


@dataclass
class WeixinAccount:
    """扫码登录后得到的机器人身份。"""

    account_id: str
    token: str
    base_url: str = ILINK_BASE_URL
    user_id: str = ""


class WeixinStore:
    """本地凭证与游标：账号、同步游标、各会话的 context_token。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "weixin"
        self._tokens: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, name: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / name

    def _read(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.warning("读取 %s 失败: %s", name, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def load_account(self) -> WeixinAccount | None:
        data = self._read("account.json")
        if not data.get("account_id") or not data.get("token"):
            return None
        return WeixinAccount(
            account_id=str(data["account_id"]),
            token=str(data["token"]),
            base_url=str(data.get("base_url") or ILINK_BASE_URL),
            user_id=str(data.get("user_id") or ""),
        )

    def save_account(self, account: WeixinAccount) -> None:
        self._write(
            "account.json",
            {
                "account_id": account.account_id,
                "token": account.token,
                "base_url": account.base_url,
                "user_id": account.user_id,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    def load_cursor(self, account_id: str) -> str:
        return str(self._read(f"{account_id}.sync.json").get("get_updates_buf") or "")

    def save_cursor(self, account_id: str, cursor: str) -> None:
        self._write(f"{account_id}.sync.json", {"get_updates_buf": cursor})

    def restore_tokens(self, account_id: str) -> None:
        data = self._read(f"{account_id}.context-tokens.json")
        self._tokens = {str(k): str(v) for k, v in data.items() if v}

    def get_token(self, chat_id: str) -> str | None:
        return self._tokens.get(chat_id)

    def set_token(self, account_id: str, chat_id: str, token: str) -> None:
        self._tokens[chat_id] = token
        self._write(f"{account_id}.context-tokens.json", dict(self._tokens))

    def clear_token(self, account_id: str, chat_id: str) -> None:
        if self._tokens.pop(chat_id, None) is not None:
            self._write(f"{account_id}.context-tokens.json", dict(self._tokens))


def extract_text(item_list: list[dict[str, Any]]) -> str:
    """从 item_list 取文本；若为引用消息，把被引用内容一并带上。"""
    for item in item_list:
        if item.get("type") != ITEM_TEXT:
            continue
        text = str((item.get("text_item") or {}).get("text") or "")
        ref = item.get("ref_msg") or {}
        ref_title = str(ref.get("title") or "").strip()
        ref_item = ref.get("message_item") or {}
        ref_text = ""
        if isinstance(ref_item, dict) and ref_item.get("type") == ITEM_TEXT:
            ref_text = str((ref_item.get("text_item") or {}).get("text") or "").strip()
        if ref_title or ref_text:
            quoted = " | ".join(part for part in (ref_title, ref_text) if part)
            return f"[引用: {quoted}]\n{text}".strip()
        return text
    return ""


class WeixinAdapter(BaseAdapter):
    """基于长轮询的微信适配器；主动推送依赖用户最近一次消息带来的 context_token。"""

    platform = "weixin"
    max_length = MAX_MESSAGE_LENGTH

    def __init__(
        self,
        secrets: Secrets,
        store: WeixinStore,
        *,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        super().__init__()
        self._secrets = secrets
        self._store = store
        self._client = client
        self._owns_client = client is None
        self._sleep = sleeper
        self._account: WeixinAccount | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._dedup = MessageDeduplicator(ttl_seconds=DEDUP_TTL)
        self._rate_events: list[float] = []
        self._cooldown_until = 0.0
        self._send_gate = asyncio.Lock()

    # ── 生命周期 ──
    async def connect(self) -> bool:
        account = self._store.load_account()
        if account is None and self._secrets.weixin_token and self._secrets.weixin_account_id:
            account = WeixinAccount(account_id=self._secrets.weixin_account_id, token=self._secrets.weixin_token)
        if account is None:
            logger.error("[weixin] 缺少凭证，请先执行 python -m newsbot login weixin")
            return False
        self._account = account
        self._store.restore_tokens(account.account_id)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        logger.info("[weixin] 已连接 account=%s", safe_id(account.account_id))
        return True

    async def disconnect(self) -> None:
        self._running = False
        task, self._poll_task = self._poll_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("[weixin] 已断开")

    # ── HTTP ──
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    async def _post(self, endpoint: str, payload: dict[str, Any], *, token: str, timeout: float) -> dict[str, Any]:
        base_url = (self._account.base_url if self._account else ILINK_BASE_URL).rstrip("/")
        body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}}, ensure_ascii=False)
        response = await asyncio.wait_for(
            self._http().post(f"{base_url}/{endpoint}", headers=_headers(token), content=body), timeout=timeout
        )
        if response.status_code >= 400:
            raise RuntimeError(f"iLink {endpoint} HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def _get(self, endpoint: str, timeout: float) -> dict[str, Any]:
        base_url = (self._account.base_url if self._account else ILINK_BASE_URL).rstrip("/")
        headers = _headers(None)
        response = await asyncio.wait_for(self._http().get(f"{base_url}/{endpoint}", headers=headers), timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    # ── 收消息 ──
    async def _poll_loop(self) -> None:
        assert self._account is not None
        cursor = self._store.load_cursor(self._account.account_id)
        failures = 0
        while self._running:
            try:
                response = await self._get_updates(cursor)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                failures += 1
                wait = FAILURE_BACKOFF if failures >= MAX_CONSECUTIVE_FAILURES else 2.0
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0
                logger.warning("[weixin] 拉取失败(%d): %s，%.0fs 后重试", failures, exc, wait)
                await self._sleep(wait)
                continue

            ret, errcode = response.get("ret", 0), response.get("errcode", 0)
            if ret not in (0, None) or errcode not in (0, None):
                if _is_session_expired(ret, errcode):
                    logger.error("[weixin] 会话过期，暂停 %.0fs", SESSION_EXPIRED_PAUSE)
                    await self._sleep(SESSION_EXPIRED_PAUSE)
                    failures = 0
                    continue
                failures += 1
                logger.warning("[weixin] getUpdates 返回 ret=%s errcode=%s (%d/3)", ret, errcode, failures)
                await self._sleep(FAILURE_BACKOFF if failures >= MAX_CONSECUTIVE_FAILURES else 2.0)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0
                continue
            failures = 0

            for message in response.get("msgs") or []:
                if isinstance(message, dict):
                    await self._process(message)
            new_cursor = str(response.get("get_updates_buf") or "")
            if new_cursor and new_cursor != cursor:
                cursor = new_cursor
                self._store.save_cursor(self._account.account_id, cursor)
            # 长轮询返回可能不含真正的挂起，显式让出一次，保证其他任务与定时器能被调度
            await asyncio.sleep(0)

    async def _get_updates(self, cursor: str) -> dict[str, Any]:
        token = self._account.token if self._account else ""
        try:
            return await self._post(EP_GET_UPDATES, {"get_updates_buf": cursor}, token=token, timeout=LONG_POLL_TIMEOUT)
        except TimeoutError:
            # 长轮询超时属正常，游标不动即可
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

    async def _process(self, message: dict[str, Any]) -> None:
        assert self._account is not None
        sender = str(message.get("from_user_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not sender or sender == self._account.account_id:
            return
        if message_id and self._dedup.is_duplicate(message_id):
            return
        item_list = [item for item in (message.get("item_list") or []) if isinstance(item, dict)]
        text = extract_text(item_list)
        # 上游会用新 message_id 重发相同内容，故再做一层内容指纹
        if text and self._dedup.is_duplicate(f"content:{sender}:{hashlib.md5(text.encode()).hexdigest()}"):
            logger.debug("[weixin] 内容重复，跳过 from=%s", safe_id(sender))
            return
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._store.set_token(self._account.account_id, sender, context_token)
        if not text:
            return
        await self.dispatch(
            MessageEvent(
                platform=self.platform,
                chat_id=sender,
                chat_type="dm",
                user_id=sender,
                text=text,
                message_id=message_id or None,
                raw=message,
            )
        )

    # ── 发消息 ──
    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        if not text.strip():
            return SendResult.success()
        if self._account is None:
            return SendResult.failure("尚未连接", retryable=True)

        chunks = self.split(text)
        last_id: str | None = None
        for index, chunk in enumerate(chunks):
            result = await self._send_chunk(chat_id, chunk)
            if not result.ok:
                return result
            last_id = result.message_id
            if index < len(chunks) - 1 and CHUNK_DELAY > 0:
                await self._sleep(CHUNK_DELAY)
        return SendResult.success(last_id)

    async def _send_chunk(self, chat_id: str, chunk: str) -> SendResult:
        assert self._account is not None
        account_id = self._account.account_id
        async with self._send_gate:
            if self._cooldown_remaining() > 0:
                return SendResult.failure(f"iLink 触发限流，冷却 {self._cooldown_remaining():.0f}s", retryable=True)
            context_token = self._store.get_token(chat_id)
            for attempt in range(CHUNK_RETRIES + 1):
                try:
                    response = await self._send_message(chat_id, chunk, context_token)
                except Exception as exc:
                    if attempt >= CHUNK_RETRIES:
                        return SendResult.failure(str(exc), retryable=True)
                    await self._sleep(RETRY_DELAY * (attempt + 1))
                    continue

                ret, errcode = response.get("ret"), response.get("errcode")
                errmsg = response.get("errmsg") or response.get("msg")
                if (ret is None or ret in (0,)) and (errcode is None or errcode in (0,)):
                    self._rate_events.clear()
                    self._cooldown_until = 0.0
                    return SendResult.success(str(response.get("message_id") or ""))

                if _is_session_expired(ret, errcode) and context_token:
                    context_token = None
                    self._store.clear_token(account_id, chat_id)
                    logger.warning("[weixin] 会话过期，去掉 context_token 重发 to=%s", safe_id(chat_id))
                    continue

                if _is_stale_session(ret, errcode, errmsg):
                    return SendResult.failure(SESSION_NOT_READY_HINT, need_user=True)

                if ret == STALE_SESSION_ERRCODE or errcode == STALE_SESSION_ERRCODE:
                    if self._record_rate_limit():
                        return SendResult.failure(f"iLink 限流，冷却 {self._cooldown_remaining():.0f}s", retryable=True)
                    if attempt >= CHUNK_RETRIES:
                        break
                    await self._sleep(RETRY_DELAY * (attempt + 1) * 3)
                    continue

                return SendResult.failure(
                    f"iLink 发送失败 ret={ret} errcode={errcode} errmsg={errmsg or '未知错误'}", retryable=True
                )
        return SendResult.failure("iLink 重试次数用尽", retryable=True)

    async def _send_message(self, chat_id: str, text: str, context_token: str | None) -> dict[str, Any]:
        assert self._account is not None
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": f"newsbot-weixin-{secrets.token_hex(8)}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        return await self._post(EP_SEND_MESSAGE, {"msg": message}, token=self._account.token, timeout=API_TIMEOUT)

    def _cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _record_rate_limit(self) -> bool:
        """记录限流事件；达到阈值即开启冷却熔断。"""
        now = time.monotonic()
        self._rate_events = [event for event in self._rate_events if now - event < RATE_LIMIT_COOLDOWN] + [now]
        if len(self._rate_events) < 2:
            return False
        self._cooldown_until = max(self._cooldown_until, now + RATE_LIMIT_COOLDOWN)
        return True


async def qr_login(
    store: WeixinStore,
    *,
    client: httpx.AsyncClient | None = None,
    bot_type: str = "3",
    timeout: float = 480.0,
    on_qr: Callable[[str], None] | None = None,
    sleeper: Callable[[float], Any] = asyncio.sleep,
) -> WeixinAccount | None:
    """扫码登录：拉取二维码 → 轮询状态 → 成功后保存凭证。"""
    owned = client is None
    http = client or httpx.AsyncClient(timeout=None)
    notify = on_qr or (lambda url: print(f"请使用微信扫描二维码：\n{url}"))
    try:
        qr = await _fetch_qr(http, bot_type)
        if not qr:
            logger.error("[weixin] 获取二维码失败")
            return None
        code, url = qr
        notify(url)
        deadline = time.monotonic() + timeout
        refreshes = 0
        while time.monotonic() < deadline:
            status = await _qr_status(http, code)
            state = str(status.get("status") or "wait")
            if state == "confirmed":
                account = WeixinAccount(
                    account_id=str(status.get("ilink_bot_id") or ""),
                    token=str(status.get("bot_token") or ""),
                    base_url=str(status.get("baseurl") or ILINK_BASE_URL),
                    user_id=str(status.get("ilink_user_id") or ""),
                )
                if not (account.account_id and account.token):
                    logger.error("[weixin] 扫码成功但凭证不完整")
                    return None
                store.save_account(account)
                logger.info("[weixin] 登录成功 account=%s", safe_id(account.account_id))
                return account
            if state == "expired":
                refreshes += 1
                if refreshes > 3:
                    logger.error("[weixin] 二维码多次过期，请重新登录")
                    return None
                qr = await _fetch_qr(http, bot_type)
                if not qr:
                    return None
                code, url = qr
                notify(url)
            await sleeper(1.0)
        logger.error("[weixin] 登录超时")
        return None
    finally:
        if owned:
            await http.aclose()


async def _fetch_qr(http: httpx.AsyncClient, bot_type: str) -> tuple[str, str] | None:
    response = await http.get(
        f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}",
        params={"bot_type": bot_type},
        headers=_headers(None),
        timeout=QR_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    code = str(data.get("qrcode") or "")
    if not code:
        return None
    return code, str(data.get("qrcode_img_content") or code)


async def _qr_status(http: httpx.AsyncClient, code: str) -> dict[str, Any]:
    response = await http.get(
        f"{ILINK_BASE_URL}/{EP_GET_QR_STATUS}",
        params={"qrcode": code},
        headers=_headers(None),
        timeout=QR_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}
