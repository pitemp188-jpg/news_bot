"""
模块: tests.fakes.adapter
职责: 平台适配器替身——记录发送内容、可注入连接失败与发送失败、可主动投喂入站消息
依赖: gateway.base
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from newsbot.gateway.base import BaseAdapter, MessageEvent, SendResult


@dataclass
class FakeAdapter(BaseAdapter):
    """模拟平台收发，供路由与场景测试使用。"""

    platform: str = "fake"
    max_length: int = 80
    connect_ok: bool = True
    send_result: SendResult | None = None
    connected: bool = False
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    connect_calls: int = 0
    disconnect_calls: int = 0

    def __post_init__(self) -> None:
        BaseAdapter.__init__(self)

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connected = self.connect_ok
        self._running = self.connect_ok
        return self.connect_ok

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False
        self._running = False

    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult:
        self.sent.append((chat_id, text, reply_to))
        if self.send_result is not None:
            return self.send_result
        return SendResult.success(f"fake-{len(self.sent)}")

    async def emit(
        self,
        text: str,
        *,
        chat_id: str = "c1",
        chat_type: str = "dm",
        user_id: str | None = None,
        message_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """投喂一条入站消息（等价于平台推送）。"""
        event = MessageEvent(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id or chat_id,
            text=text,
            message_id=message_id,
            timestamp=datetime.now(),
            raw=raw or {},
        )
        await self.dispatch(event)
