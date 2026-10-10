"""
模块: gateway.qqbot
职责: QQ 官方机器人适配器——获取 token、WebSocket 收发、心跳与断线重连、文本发送
依赖: gateway.base, core.config, core.errors, core.log
来源: 精简自 hermes-agent@908e4a4 gateway/platforms/qqbot/{adapter.py,constants.py}（MIT），
      删除富媒体上传、语音识别、按钮交互、频道消息与多账号
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from newsbot.core.config import Secrets
from newsbot.core.log import get_logger
from newsbot.gateway.base import BaseAdapter, MessageEvent, SendResult, safe_id

logger = get_logger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
GATEWAY_PATH = "/gateway"

MAX_MESSAGE_LENGTH = 1500
API_TIMEOUT = 15.0
CONNECT_TIMEOUT = 15.0
# 只订阅单聊与群 @ 消息，避免因权限不足触发 4014
INTENTS = 1 << 25
RECONNECT_BACKOFF = (2, 5, 15, 30, 60)
# 连续重连到这个次数之后只把日志降级为 warning，**不停止重连**（见 _listen_loop）
RECONNECT_WARN_ATTEMPTS = 5
QUICK_DISCONNECT_SECONDS = 5.0
MAX_QUICK_DISCONNECTS = 3
RATE_LIMIT_WAIT = 30.0
TOKEN_EARLY_REFRESH = 60.0

MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
OP_HELLO, OP_IDENTIFY, OP_HEARTBEAT, OP_RESUME = 10, 2, 1, 6
OP_DISPATCH, OP_HEARTBEAT_ACK, OP_RECONNECT, OP_INVALID_SESSION = 0, 11, 7, 9

# 不可恢复的关闭码：权限、沙箱、封禁等，继续重连没有意义
FATAL_CLOSE_CODES = {
    4001: "无效操作码",
    4002: "无效载荷",
    4010: "无效分片",
    4011: "需要分片",
    4012: "API 版本不支持",
    4013: "无效 intents",
    4014: "intents 未授权",
    4914: "机器人已下线或仅沙箱",
    4915: "机器人已被封禁",
}
# 会话失效的关闭码：清空 session 后重新 Identify
SESSION_INVALID_CLOSE_CODES = {4006, 4007} | set(range(4900, 4914))

INBOUND_HANDLERS = {
    "C2C_MESSAGE_CREATE": "c2c",
    "GROUP_AT_MESSAGE_CREATE": "group",
}


class QQCloseError(Exception):
    """WebSocket 关闭，携带关闭码用于重连判定。"""

    def __init__(self, code: int | None, reason: str = "") -> None:
        self.code = int(code) if code else None
        self.reason = str(reason or "")
        super().__init__(f"WebSocket 关闭 code={self.code} reason={self.reason}")


class QQBotAdapter(BaseAdapter):
    """基于官方 WebSocket 网关 + REST 的 QQ 机器人适配器。"""

    platform = "qqbot"
    max_length = MAX_MESSAGE_LENGTH

    def __init__(
        self,
        secrets: Secrets,
        *,
        http: httpx.AsyncClient | None = None,
        connect_ws: Any | None = None,
    ) -> None:
        super().__init__()
        self._app_id = secrets.qq_app_id.strip()
        self._secret = secrets.qq_client_secret.strip()
        self._http = http
        self._owns_http = http is None
        self._connect_ws = connect_ws  # 测试注入；默认用 websockets 库
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._ws: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval = 30.0
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._chat_types: dict[str, str] = {}
        self._refreshing = False

    # ── 对外接口 ──
    async def connect(self) -> bool:
        if not (self._app_id and self._secret):
            logger.error("[qqbot] 缺少 QQ_APP_ID / QQ_CLIENT_SECRET，无法启动")
            return False
        try:
            await self._ensure_token()
            await self._open_ws()
        except Exception as exc:
            logger.error("[qqbot] 连接失败: %s", exc)
            return False
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop(), name="qqbot-listen")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="qqbot-heartbeat")
        logger.info("[qqbot] 已连接 app_id=%s", safe_id(self._app_id))
        return True

    async def disconnect(self) -> None:
        self._running = False
        for task in (self._listen_task, self._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._listen_task = self._heartbeat_task = None
        await self._close_ws()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        self._session_id = None
        self._last_seq = None
        logger.info("[qqbot] 已断开")

    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        if not text.strip():
            return SendResult.success()
        if not self._running:
            return SendResult.failure("尚未连接", retryable=True)
        chat_type = self._chat_types.get(chat_id, "c2c")
        kind = "users" if chat_type == "c2c" else "groups"
        last_id: str | None = None
        for chunk in self.split(text):
            body: dict[str, Any] = {
                "content": chunk,
                "msg_type": MSG_TYPE_TEXT,
                "msg_seq": self._next_seq(),
            }
            if reply_to:
                body["msg_id"] = reply_to
            try:
                data = await self._api("POST", f"/v2/{kind}/{chat_id}/messages", body)
            except Exception as exc:
                return SendResult.failure(str(exc), retryable=self._is_retryable(exc), need_user=self._is_limited(exc))
            last_id = str(data.get("id") or "")
            reply_to = None  # 只有首段引用原消息
        return SendResult.success(last_id)

    # ── token 与 HTTP ──
    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_EARLY_REFRESH:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_expires_at - TOKEN_EARLY_REFRESH:
                return self._token
            client = self._client()
            response = await client.post(
                TOKEN_URL, json={"appId": self._app_id, "clientSecret": self._secret}, timeout=API_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"获取 token 失败: {data}")
            self._token = str(token)
            expires_in = data.get("expires_in")
            self._token_expires_at = time.time() + (7200.0 if expires_in is None else float(expires_in))
            logger.info("[qqbot] access token 已刷新")
            return self._token

    async def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._ensure_token()
        response = await self._client().request(
            method,
            f"{API_BASE}{path}",
            json=body,
            headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
            timeout=API_TIMEOUT,
        )
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            message = data.get("message") or data.get("msg") or response.text[:120]
            raise RuntimeError(f"QQ 接口错误 [{response.status_code}] {path}: {message}")
        return data if isinstance(data, dict) else {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=API_TIMEOUT)
        return self._http

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        text = str(exc)
        return any(marker in text for marker in ("timeout", "超时", "500", "502", "503", "504"))

    @staticmethod
    def _is_limited(exc: Exception) -> bool:
        """主动消息受限：需要用户先与机器人交互。"""
        text = str(exc).lower()
        return any(marker in text for marker in ("msg_id", "主动", "no permission", "forbidden", "403"))

    @staticmethod
    def _next_seq() -> int:
        return random.randint(0, 65535)

    # ── WebSocket ──
    async def _open_ws(self) -> None:
        await self._close_ws()
        data = await self._api("GET", GATEWAY_PATH)
        url = data.get("url")
        if not url:
            raise RuntimeError(f"网关地址缺失: {data}")
        self._ws = await self._open_connection(url)
        logger.debug("[qqbot] WebSocket 已建立")

    async def _open_connection(self, url: str) -> Any:
        if self._connect_ws is not None:
            return await self._connect_ws(url)
        import websockets

        return await websockets.connect(url, open_timeout=CONNECT_TIMEOUT, ping_interval=None)

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _listen_loop(self) -> None:
        backoff_index = 0
        quick_disconnects = 0
        while self._running:
            started = time.monotonic()
            try:
                await self._read_events()
                backoff_index = quick_disconnects = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return
                logger.warning("[qqbot] WebSocket 关闭 code=%s reason=%s", exc.code, exc.reason)
                if time.monotonic() - started < QUICK_DISCONNECT_SECONDS:
                    quick_disconnects += 1
                    if quick_disconnects >= MAX_QUICK_DISCONNECTS:
                        logger.error("[qqbot] 反复快速断开，请检查 AppID/Secret 与机器人权限")
                        self._running = False
                        return
                else:
                    # 连接实打实用了一段时间才断：这是网络抖动，不是配置问题，
                    # 重连计数必须清零。不清零的话计数会跨"多次健康连接"累加，
                    # 挂机一整天（休眠唤醒、网关滚动重启）攒够 5 次就永久离线了。
                    quick_disconnects = 0
                    backoff_index = 0
                if exc.code in FATAL_CLOSE_CODES:
                    logger.error("[qqbot] 不可恢复关闭码 %s: %s", exc.code, FATAL_CLOSE_CODES[exc.code])
                    self._running = False
                    return
                if exc.code == 4004:
                    logger.info("[qqbot] token 失效，刷新后重连")
                    self._token = None
                    self._token_expires_at = 0.0
                if exc.code in SESSION_INVALID_CLOSE_CODES:
                    logger.info("[qqbot] 会话失效（%s），重新 Identify", exc.code)
                    self._session_id = None
                    self._last_seq = None
                if exc.code == 4008:
                    logger.info("[qqbot] 触发限流，等待 %.0fs", RATE_LIMIT_WAIT)
                    await asyncio.sleep(RATE_LIMIT_WAIT)
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[qqbot] WebSocket 异常: %s", exc)
            if backoff_index >= RECONNECT_WARN_ATTEMPTS:
                # 只降级日志，**绝不放弃重连**：这个进程要挂机跑一整天，必须能自愈。
                # 原来这里是"超过 5 次就 self._running = False 退出循环"，而计数只在
                # 连接正常返回时才清零，于是 24 小时里攒够 5 次抖动（休眠唤醒、网关
                # 滚动重启）机器人就永久离线——**进程还活着**，只看进程存活的监控
                # 根本发现不了。真正需要"停机"的信号是"反复瞬间断开"（配置/权限错，
                # 由 MAX_QUICK_DISCONNECTS 处理）与不可恢复关闭码（FATAL_CLOSE_CODES）。
                logger.warning("[qqbot] 已连续重连 %d 次仍未恢复，继续重试", backoff_index)
            delay = RECONNECT_BACKOFF[min(backoff_index, len(RECONNECT_BACKOFF) - 1)]
            backoff_index += 1
            logger.info("[qqbot] %.0fs 后重连（第 %d 次）", delay, backoff_index)
            await asyncio.sleep(delay)
            try:
                await self._open_ws()
            except Exception as exc:
                logger.warning("[qqbot] 重连失败: %s", exc)

    async def _read_events(self) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("WebSocket 未连接")
        while self._running and self._ws is ws:
            raw = await ws.recv()
            if raw is None:  # 对端关闭
                raise QQCloseError(None, "connection closed")
            self._handle_payload(raw)

    def _handle_payload(self, raw: str | bytes) -> None:
        """解析一帧；心跳、Identify、Resume 由本方法负责。"""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[qqbot] 无法解析的帧: %r", str(raw)[:120])
            return
        if not isinstance(payload, dict):
            return
        op, event, seq, data = payload.get("op"), payload.get("t"), payload.get("s"), payload.get("d")
        if isinstance(seq, int) and (self._last_seq is None or seq > self._last_seq):
            self._last_seq = seq
        if op == OP_HELLO:
            interval = (data or {}).get("heartbeat_interval", 30000) if isinstance(data, dict) else 30000
            self._heartbeat_interval = max(5.0, float(interval) / 1000 * 0.8)
            self._spawn(
                self._send_resume() if (self._session_id and self._last_seq is not None) else self._send_identify()
            )
        elif op == OP_DISPATCH and event:
            if event == "READY" and isinstance(data, dict):
                self._session_id = data.get("session_id")
                logger.info("[qqbot] 已就绪 session=%s", safe_id(self._session_id))
            elif event in INBOUND_HANDLERS:
                self._spawn(self._on_message(event, data if isinstance(data, dict) else {}))
        elif op == OP_RECONNECT:
            logger.info("[qqbot] 服务端要求重连")
            self._spawn(self._close_ws())
        elif op == OP_INVALID_SESSION:
            if not data:
                self._session_id = None
                self._last_seq = None
            self._spawn(self._close_ws())
        elif op in (OP_HEARTBEAT_ACK,):
            return

    @staticmethod
    def _spawn(coro: Any) -> None:
        """当前无事件循环时（同步测试）丢弃协程，避免报警告。"""
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            with contextlib.suppress(Exception):
                coro.close()

    async def _send_identify(self) -> None:
        token = await self._ensure_token()
        await self._send_json(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {"$os": "windows", "$browser": "newsbot", "$device": "newsbot"},
                },
            }
        )
        logger.debug("[qqbot] 已发送 Identify")

    async def _send_resume(self) -> None:
        token = await self._ensure_token()
        sent = await self._send_json(
            {
                "op": OP_RESUME,
                "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._last_seq},
            }
        )
        if not sent:
            self._session_id = None
            self._last_seq = None
        else:
            logger.debug("[qqbot] 已发送 Resume")

    async def _send_json(self, payload: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            logger.warning("[qqbot] 发送失败: %s", exc)
            return False

    async def _heartbeat_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                await self._send_json({"op": OP_HEARTBEAT, "d": self._last_seq})

    # ── 入站消息 ──
    async def _on_message(self, event: str, data: dict[str, Any]) -> None:
        raw_author = data.get("author")
        author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
        text = str(data.get("content") or "").strip()
        if event == "C2C_MESSAGE_CREATE":
            chat_id = str(author.get("user_openid") or "")
            chat_type = "dm"
            user_id = chat_id
        else:
            chat_id = str(data.get("group_openid") or "")
            chat_type = "group"
            user_id = str(author.get("member_openid") or "")
            text = _strip_mention(text)
        if not chat_id or not text:
            return
        self._chat_types[chat_id] = "c2c" if chat_type == "dm" else "group"
        await self.dispatch(
            MessageEvent(
                platform=self.platform,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=user_id or chat_id,
                text=text,
                message_id=str(data.get("id") or "") or None,
                timestamp=_parse_timestamp(data.get("timestamp")),
                raw=data,
            )
        )


def _strip_mention(text: str) -> str:
    """群聊消息里的 @机器人 前缀不参与指令解析。"""
    if text.startswith("@"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    return text


def _parse_timestamp(raw: Any) -> datetime:
    """QQ 时间戳现为 ISO 8601，历史格式为毫秒整数。"""
    if isinstance(raw, str) and raw:
        with contextlib.suppress(ValueError, TypeError):
            return datetime.fromisoformat(raw)
    if raw:
        with contextlib.suppress(ValueError, TypeError):
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).astimezone()
    return datetime.now()
