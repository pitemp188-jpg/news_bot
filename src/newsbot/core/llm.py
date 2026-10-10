"""
模块: core.llm
职责: LLM 客户端——OpenAI 兼容接口、工具调用、瞬时错误重试与用量返回
依赖: core.config, core.errors, core.log
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from newsbot.core.config import Secrets
from newsbot.core.errors import AuthError, FatalError, NewsbotError, RetriableError
from newsbot.core.log import get_logger
from newsbot.core.models import LlmUsage

logger = get_logger(__name__)

RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """一次模型调用的用量，供成本统计落库。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


@dataclass
class LLMReply:
    """一次模型回复。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    def usage(self) -> Usage:
        return Usage(prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens, model=self.model)


class LLM(Protocol):
    """Agent 与结果处理共用的模型接口，便于测试替换。

    `max_tokens` 用于**机械型任务**（例如把讲同一件事的条目分组）：这类任务的输出
    本该只有几个字符（`3,7`）。当前的模型是**推理模型**，同一件事它会先烧掉两千多个
    推理 token 才给结论——实测一次分组用掉 2588 个 completion token，耗时 4.5～170s
    不等。给出上限能把最坏情况压住。

    注意上限给太小是有害的：推理 token 与输出 token 共用这个额度，实测上限 1000 时
    推理把额度吃光、正文直接为空（调用成功但拿不到任何结果）。所以要留出余量。
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMReply: ...

    async def aclose(self) -> None: ...


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("工具参数不是合法 JSON: %s", raw[:120])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_reply(response: Any) -> LLMReply:
    message = response.choices[0].message
    calls = [
        ToolCall(id=call.id, name=call.function.name, arguments=_parse_arguments(call.function.arguments))
        for call in (message.tool_calls or [])
    ]
    usage = response.usage
    return LLMReply(
        content=message.content or "",
        tool_calls=calls,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        model=str(getattr(response, "model", "") or ""),
    )


def classify_error(exc: Exception) -> type[NewsbotError]:
    """把模型 SDK 的异常映射为本项目的错误类型；已是本项目错误时保持原类型。"""
    if isinstance(exc, NewsbotError):
        return type(exc)
    if isinstance(exc, RETRYABLE_ERRORS):
        return RetriableError
    if isinstance(exc, APIStatusError):
        return AuthError if exc.status_code in (401, 403) else FatalError
    return FatalError


class OpenAIClient:
    """OpenAI 兼容客户端，带重试与错误分类；client 可注入以便测试。"""

    def __init__(
        self, secrets: Secrets, *, client: Any | None = None, max_retries: int = 3, timeout: float = 120.0
    ) -> None:
        self._model = secrets.llm_model
        self._max_retries = max_retries
        self._client = client or AsyncOpenAI(
            base_url=secrets.llm_base_url, api_key=secrets.llm_api_key, timeout=timeout, max_retries=0
        )

    async def _call(self, payload: dict[str, Any]) -> Any:
        try:
            return await self._client.chat.completions.create(**payload)
        except RETRYABLE_ERRORS as exc:
            raise RetriableError(f"模型瞬时故障: {exc}") from exc
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise AuthError(f"模型鉴权失败: {exc}") from exc
            raise FatalError(f"模型返回错误 {exc.status_code}: {exc}") from exc
        except OpenAIError as exc:
            raise FatalError(f"模型调用失败: {exc}") from exc

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMReply:
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens is not None:
            # 必须用 max_completion_tokens：实测火山方舟这个端点**忽略 max_tokens**
            # （传 50 仍然返回 3455 个 completion token），只认 max_completion_tokens
            payload["max_completion_tokens"] = max_tokens

        last_error: NewsbotError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return _to_reply(await self._call(payload))
            except RetriableError as exc:
                last_error = exc
                wait = float(2**attempt)
                logger.warning("LLM 调用失败(%d/%d): %s，%.0fs 后重试", attempt, self._max_retries, exc, wait)
                await asyncio.sleep(wait)
        raise RetriableError(f"LLM 重试 {self._max_retries} 次仍失败: {last_error}")

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            await close()


async def record_usage(session: AsyncSession, reply: LLMReply, *, task_id: int | None = None, model: str = "") -> None:
    """把一次模型调用的用量写入 llm_usage，供成本统计与预算控制。"""
    session.add(
        LlmUsage(
            task_id=task_id,
            model=model,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
    )
    await session.commit()
