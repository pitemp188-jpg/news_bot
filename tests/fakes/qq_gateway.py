"""
模块: tests.fakes.qq_gateway
职责: QQ WebSocket 替身——按脚本吐出帧、记录已发送帧、支持注入关闭
依赖: 无
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class FakeWS:
    """模拟 websockets 连接对象。"""

    frames: list[object] = field(default_factory=list)
    sent: list[dict[str, object]] = field(default_factory=list)
    closed: bool = False

    async def recv(self) -> str | None:
        frame = self.frames.pop(0) if self.frames else None
        if isinstance(frame, Exception):
            raise frame
        return frame  # type: ignore[return-value]

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


def hello(interval_ms: int = 30000) -> str:
    return json.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}})


def ready(session_id: str = "sess-1") -> str:
    return json.dumps({"op": 0, "t": "READY", "s": 1, "d": {"session_id": session_id}})


def c2c_message(text: str = "你好", msg_id: str = "m1", openid: str = "u1") -> str:
    return json.dumps(
        {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "s": 2,
            "d": {
                "id": msg_id,
                "content": text,
                "author": {"user_openid": openid},
                "timestamp": "2026-10-09T12:00:00+08:00",
            },
        }
    )


def group_message(text: str = "@机器人 今天新闻", msg_id: str = "m2", group: str = "g1", member: str = "u2") -> str:
    return json.dumps(
        {
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "s": 3,
            "d": {"id": msg_id, "content": text, "group_openid": group, "author": {"member_openid": member}},
        }
    )


def control(op: int, data: object = None) -> str:
    return json.dumps({"op": op, "d": data})
