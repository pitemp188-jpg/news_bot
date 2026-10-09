"""
模块: gateway.base
职责: 适配器契约——统一入站事件、发送结果、抽象基类与分段工具
依赖: core.errors, core.log
来源: 精简自 hermes-agent@908e4a4 gateway/platforms/{event.py,base.py,helpers.py}（MIT）
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from newsbot.core.log import get_logger

logger = get_logger(__name__)

# 长度预留：拼接关闭围栏与分页标记时用
INDICATOR_RESERVE = 10
FENCE_CLOSE = "\n```"


@dataclass
class MessageEvent:
    """所有平台统一的入站消息。"""

    platform: str
    chat_id: str
    chat_type: str  # dm | group
    user_id: str
    text: str
    message_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "text": self.text,
            "message_id": self.message_id,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class SendResult:
    """一次发送的结果。"""

    ok: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    # 平台要求用户先发消息（微信会话未就绪 / QQ 主动消息受限），交给 router 补投递
    need_user: bool = False

    @classmethod
    def failure(cls, error: str, *, retryable: bool = False, need_user: bool = False) -> SendResult:
        return cls(ok=False, error=error, retryable=retryable, need_user=need_user)

    @classmethod
    def success(cls, message_id: str | None = None) -> SendResult:
        return cls(ok=True, message_id=message_id)


def safe_id(value: str | None, keep: int = 8) -> str:
    """日志里只保留 id 前几位，避免完整用户标识外泄。"""
    raw = str(value or "").strip()
    return raw[:keep] if raw else "?"


def _pick_split(text: str, limit: int) -> int:
    """在 limit 内挑一个尽量自然的断点。"""
    window = text[:limit]
    for separator in ("\n\n", "\n", "。"):
        index = window.rfind(separator)
        if index >= limit // 2:
            return index + len(separator)
    index = window.rfind(" ")
    return index if index >= limit // 2 else limit


def _ends_inside_fence(chunk: str) -> tuple[bool, str]:
    """判断片段是否停在未闭合的代码块内，并返回该块的语言标记。"""
    inside = False
    language = ""
    for line in chunk.splitlines():
        if line.lstrip().startswith("```"):
            if inside:
                inside, language = False, ""
            else:
                inside, language = True, line.lstrip()[3:].strip()
    return inside, language


def split_text(text: str, limit: int) -> list[str]:
    """
    按上限切分文本：优先在段落 / 行 / 句号处断开，不拆开代码块，也不切断代理对。
    多段时在末尾标注 (n/m)。
    """
    if limit <= 0:
        return [text]
    if len(text) <= limit:
        return [text]

    # 平台上限很小时不再追加 (n/m)，否则预留位反而挤掉正文
    use_indicator = limit >= 40
    reserve = INDICATOR_RESERVE if use_indicator else 0
    chunks: list[str] = []
    remaining = text
    carry_language: str | None = None

    while remaining:
        prefix = f"```{carry_language}\n" if carry_language is not None else ""
        starts_inside = carry_language is not None
        budget = limit - reserve - len(prefix) - len(FENCE_CLOSE)
        if budget < 1:
            budget = max(1, limit // 2)

        if len(prefix) + len(remaining) <= limit - reserve:
            body_inside, _ = _ends_inside_fence(remaining)
            chunks.append(prefix + remaining + (FENCE_CLOSE if starts_inside != body_inside else ""))
            break

        cut = _pick_split(remaining, budget)
        body = remaining[:cut]
        remaining = remaining[cut:].lstrip()
        body_inside, body_language = _ends_inside_fence(body)
        # 片段起始处若已处于代码块内，块内外状态需与片段自身的奇偶翻转叠加
        ends_inside = starts_inside != body_inside
        chunks.append(prefix + body + (FENCE_CLOSE if ends_inside else ""))
        carry_language = (body_language or carry_language) if ends_inside else None

    if use_indicator and len(chunks) > 1:
        chunks = [f"{chunk} ({index}/{len(chunks)})" for index, chunk in enumerate(chunks, 1)]
    return chunks


class MessageDeduplicator:
    """按 message_id 去重；微信上游会用新 id 重发同一内容，故支持内容指纹。"""

    def __init__(self, *, ttl_seconds: float = 300.0, max_size: int = 5000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._seen: dict[str, float] = {}

    def is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._seen) > self._max_size:
            self._purge(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def _purge(self, now: float) -> None:
        expired = [key for key, seen_at in self._seen.items() if now - seen_at > self._ttl]
        for key in expired:
            self._seen.pop(key, None)
        if len(self._seen) > self._max_size:
            for key in sorted(self._seen, key=self._seen.get)[: len(self._seen) - self._max_size]:  # type: ignore[arg-type]
                self._seen.pop(key, None)


class BaseAdapter(ABC):
    """平台适配器基类：连接、收消息回调、发送。"""

    platform: str = ""
    max_length: int = 2000
    max_message_length: int = 2000

    def __init__(self) -> None:
        self._handler: Callable[[MessageEvent], Awaitable[None]] | None = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self._running

    def set_handler(self, handler: Callable[[MessageEvent], Awaitable[None]]) -> None:
        """注册入站回调；未注册时丢弃消息，避免静默堆积。"""
        self._handler = handler

    async def dispatch(self, event: MessageEvent) -> None:
        """把入站事件交给回调，异常只记录不上抛，保证收消息循环不中断。"""
        if self._handler is None:
            logger.warning("[%s] 未注册入站回调，丢弃消息 from=%s", self.platform, safe_id(event.user_id))
            return
        try:
            await self._handler(event)
        except Exception:
            logger.exception("[%s] 入站回调处理异常 from=%s", self.platform, safe_id(event.user_id))

    def split(self, text: str) -> list[str]:
        return split_text(text, self.max_length)

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接并开始收消息；返回是否成功。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接并释放资源。"""

    @abstractmethod
    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        """发送纯文本，超出上限时分段。"""

    async def aclose(self) -> None:
        if self._running:
            await self.disconnect()
