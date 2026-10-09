"""
模块: tests.unit.core.test_llm
职责: 校验应答解析、工具调用解析、错误分类、重试与不重试分支
依赖: core.llm
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai import OpenAIError

from newsbot.core import llm as llm_module
from newsbot.core.config import Secrets
from newsbot.core.errors import FatalError, RetriableError
from newsbot.core.llm import OpenAIClient, classify_error


def _secrets() -> Secrets:
    return Secrets(llm_api_key="test", llm_base_url="https://api.deepseek.com/v1", llm_model="deepseek-chat")


def _response(
    content: str | None = "", tool_calls: list[Any] | None = None, *, prompt: int = 3, completion: int = 2
) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _stub_client(*results: Any) -> tuple[Any, list[dict[str, Any]]]:
    """只实现 chat.completions.create 的假客户端，结果按顺序返回或抛出。"""
    payloads: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        payloads.append(kwargs)
        result = results[min(len(payloads) - 1, len(results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result

    chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return SimpleNamespace(chat=chat), payloads


async def test_returns_content_and_usage() -> None:
    stub, payloads = _stub_client(_response("你好"))
    reply = await OpenAIClient(_secrets(), client=stub).complete([{"role": "user", "content": "hi"}])
    assert reply.content == "你好"
    assert reply.prompt_tokens == 3
    assert reply.tool_calls == []
    assert payloads[0]["model"] == "deepseek-chat"
    assert "tools" not in payloads[0]


async def test_parses_tool_calls_and_passes_tools() -> None:
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="search", arguments='{"query": "AI"}'))
    stub, payloads = _stub_client(_response(None, [call]))
    reply = await OpenAIClient(_secrets(), client=stub).complete(
        [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
    )
    assert reply.content == ""
    assert reply.tool_calls[0].name == "search"
    assert reply.tool_calls[0].arguments == {"query": "AI"}
    assert payloads[0]["tool_choice"] == "auto"


async def test_broken_arguments_fall_back_to_empty() -> None:
    call = SimpleNamespace(id="c", function=SimpleNamespace(name="fetch", arguments="{bad"))
    stub, _ = _stub_client(_response("", [call]))
    reply = await OpenAIClient(_secrets(), client=stub).complete([{"role": "user", "content": "hi"}])
    assert reply.tool_calls[0].arguments == {}


async def test_missing_usage_defaults_to_zero() -> None:
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    stub, _ = _stub_client(response)
    reply = await OpenAIClient(_secrets(), client=stub).complete([{"role": "user", "content": "hi"}])
    assert (reply.prompt_tokens, reply.completion_tokens) == (0, 0)


async def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)
    stub, payloads = _stub_client(RetriableError("瞬时故障"), _response("ok"))
    reply = await OpenAIClient(_secrets(), client=stub).complete([{"role": "user", "content": "hi"}])
    assert reply.content == "ok"
    assert len(payloads) == 2


async def test_retry_exhausted_raises_retriable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)
    stub, payloads = _stub_client(RetriableError("一直失败"))
    with pytest.raises(RetriableError):
        await OpenAIClient(_secrets(), client=stub, max_retries=2).complete([{"role": "user", "content": "hi"}])
    assert len(payloads) == 2


async def test_fatal_error_is_not_retried() -> None:
    stub, payloads = _stub_client(OpenAIError("boom"))
    with pytest.raises(FatalError):
        await OpenAIClient(_secrets(), client=stub).complete([{"role": "user", "content": "hi"}])
    assert len(payloads) == 1


async def test_close_is_optional() -> None:
    stub, _ = _stub_client(_response("ok"))
    await OpenAIClient(_secrets(), client=stub).aclose()

    closed: list[bool] = []

    class _Closable:
        async def close(self) -> None:
            closed.append(True)

    await OpenAIClient(_secrets(), client=_Closable()).aclose()
    assert closed == [True]


@pytest.mark.parametrize("exc", [OpenAIError("x"), ValueError("x")])
def test_classify_unknown_errors_are_fatal(exc: Exception) -> None:
    assert classify_error(exc) is FatalError


async def test_fake_llm_returns_scripted_replies() -> None:
    from tests.fakes.llm import FakeLLM, reply

    fake = FakeLLM(replies=[reply("一"), reply("二")])
    assert (await fake.complete([])).content == "一"
    assert (await fake.complete([])).content == "二"
    assert (await fake.complete([])).content == ""
    assert len(fake.calls) == 3
    await fake.aclose()
    assert fake.closed


async def test_record_usage_persists_tokens(tmp_path: Path) -> None:
    from sqlalchemy import func, select

    from newsbot.core.db import Database
    from newsbot.core.llm import LLMReply, record_usage
    from newsbot.core.models import LlmUsage, Task

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    await db.init()
    try:
        async with db.session() as session:
            task = Task(kind="chat", query="q")
            session.add(task)
            await session.commit()
            await record_usage(session, LLMReply(prompt_tokens=7, completion_tokens=5), task_id=task.id, model="m")
        async with db.session() as session:
            total = (await session.execute(select(func.count()).select_from(LlmUsage))).scalar_one()
            row = (await session.execute(select(LlmUsage))).scalar_one()
        assert total == 1
        assert (row.prompt_tokens, row.completion_tokens) == (7, 5)
        assert row.task_id is not None
    finally:
        await db.dispose()
