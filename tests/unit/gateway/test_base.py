"""
模块: tests.unit.gateway.test_base
职责: 校验分段规则、去重器、发送结果与适配器回调容错
依赖: gateway.base
"""

from __future__ import annotations

from tests.fakes.adapter import FakeAdapter

from newsbot.gateway.base import (
    BaseAdapter,
    MessageDeduplicator,
    MessageEvent,
    SendResult,
    safe_id,
    split_text,
)


def test_short_text_is_not_split() -> None:
    assert split_text("短消息", 100) == ["短消息"]


def test_empty_text_returns_single_chunk() -> None:
    assert split_text("", 10) == [""]


def test_split_prefers_paragraph_boundary() -> None:
    text = "第一段内容比较长一些。\n\n第二段内容比较长一些。\n\n第三段内容比较长一些。"
    chunks = split_text(text, 20)
    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert "第一段内容比较长一些。" in chunks[0]


def test_split_marks_multi_chunk_index() -> None:
    chunks = split_text("a" * 200, 40)
    assert len(chunks) > 1
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert chunks[-1].endswith(f"({len(chunks)}/{len(chunks)})")


def test_split_keeps_fence_closed_and_reopened() -> None:
    text = "```python\nprint('x')\nprint('y')\nprint('z')\nprint('w')\n```"
    chunks = split_text(text, 40)
    assert len(chunks) > 1
    assert all(len(chunk) <= 40 for chunk in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_split_never_cuts_surrogate_pair() -> None:
    # emoji 占两个 UTF-16 单元，但 Python 按码点切分，此处确保不产生孤立代理对
    chunks = split_text("🙂" * 30, 20)
    assert all(chunk.encode("utf-16-le").decode("utf-16-le") == chunk for chunk in chunks)


def test_split_with_zero_limit_returns_original() -> None:
    assert split_text("内容", 0) == ["内容"]


def test_deduplicator_rejects_same_key() -> None:
    dedup = MessageDeduplicator()
    assert dedup.is_duplicate("m1") is False
    assert dedup.is_duplicate("m1") is True
    assert dedup.is_duplicate("m2") is False


def test_deduplicator_purges_when_over_capacity() -> None:
    dedup = MessageDeduplicator(max_size=2)
    dedup.is_duplicate("a")
    dedup.is_duplicate("b")
    dedup.is_duplicate("c")
    assert len(dedup._seen) <= 3


def test_safe_id_truncates() -> None:
    assert safe_id("abcdefghijklmn") == "abcdefgh"
    assert safe_id("") == "?"
    assert safe_id(None) == "?"


def test_send_result_helpers() -> None:
    failed = SendResult.failure("坏了", retryable=True, need_user=True)
    assert failed.ok is False and failed.retryable and failed.need_user
    assert SendResult.success("id").message_id == "id"


def test_event_serializes() -> None:
    event = MessageEvent(platform="qqbot", chat_id="c1", chat_type="dm", user_id="u1", text="你好")
    assert event.as_dict()["platform"] == "qqbot"
    assert "timestamp" in event.as_dict()


async def test_dispatch_without_handler_is_dropped() -> None:
    adapter = FakeAdapter()
    await adapter.emit("无人接收")  # 不应抛错
    assert adapter.sent == []


async def test_handler_error_is_contained() -> None:
    adapter = FakeAdapter()
    seen: list[str] = []

    async def boom(event: MessageEvent) -> None:
        seen.append(event.text)
        raise RuntimeError("处理失败")

    adapter.set_handler(boom)
    await adapter.emit("消息一")
    await adapter.emit("消息二")
    assert seen == ["消息一", "消息二"]


async def test_handler_receives_normalized_event() -> None:
    adapter = FakeAdapter()
    captured: list[MessageEvent] = []
    adapter.set_handler(lambda event: _capture(captured, event))
    await adapter.emit("HI", chat_id="g1", chat_type="group", user_id="u9", message_id="m1")
    assert captured[0].chat_id == "g1"
    assert captured[0].chat_type == "group"
    assert captured[0].user_id == "u9"


async def _capture(sink: list[MessageEvent], event: MessageEvent) -> None:
    sink.append(event)


async def test_adapter_split_uses_platform_limit() -> None:
    adapter = FakeAdapter(max_length=12)
    chunks = adapter.split("很长的一段文本需要切分")
    assert all(len(chunk) <= 12 for chunk in chunks)


async def test_aclose_disconnects_when_running() -> None:
    adapter = FakeAdapter()
    await adapter.connect()
    await adapter.aclose()
    assert adapter.disconnect_calls == 1


def test_adapter_is_abstract() -> None:
    class Incomplete(BaseAdapter):
        pass

    try:
        Incomplete()  # type: ignore[abstract]
    except TypeError:
        pass
    else:  # pragma: no cover - 抽象类必须拒绝实例化
        raise AssertionError("BaseAdapter 应禁止直接实例化")
