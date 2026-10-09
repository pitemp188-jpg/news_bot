"""
模块: tests.live.test_live_model
职责: 真实模型连通性——纯文本、tool-calling、用量统计与错误分类
依赖: core.config, core.llm

运行前置：`.env` 中配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，然后
`uv run python scripts/check.py --live` 或 `uv run python -m pytest -m live`。
"""

from __future__ import annotations

import pytest

from newsbot.core.config import Secrets, load_config
from newsbot.core.db import Database
from newsbot.core.llm import OpenAIClient
from newsbot.core.models import LlmUsage

pytestmark = pytest.mark.live

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "搜索互联网",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    }
]


def _secrets() -> Secrets:
    """从 .env 读取真实凭证；未配置时跳过（CI 无密钥场景）。"""
    secrets = load_config().secrets
    if not secrets.llm_api_key:
        pytest.skip("未配置 LLM_API_KEY，跳过 live 测试")
    return secrets


async def test_live_model_replies_plain_text() -> None:
    """真实模型能返回非空文本，并给出 token 用量。"""
    client = OpenAIClient(_secrets(), max_retries=2)
    try:
        reply = await client.complete(
            [
                {"role": "system", "content": "你是简洁的助手，只按要求输出，不要解释。"},
                {"role": "user", "content": "只回复这三个字：已连通"},
            ]
        )
    finally:
        await client.aclose()

    assert "已连通" in reply.content
    assert reply.prompt_tokens > 0 and reply.completion_tokens > 0


async def test_live_model_calls_tool() -> None:
    """真实模型在需要联网时返回合规的 tool_call：函数名与参数都要正确。"""
    client = OpenAIClient(_secrets(), max_retries=2)
    try:
        reply = await client.complete(
            [{"role": "user", "content": "用 search 工具查一下今天的 AI 新闻，先不要直接回答。"}],
            tools=TOOL_SCHEMA,
        )
    finally:
        await client.aclose()

    assert reply.tool_calls, f"模型未发起工具调用: {reply}"
    call = reply.tool_calls[0]
    assert call.name == "search"
    assert isinstance(call.arguments.get("query"), str) and call.arguments["query"].strip()


async def test_live_model_answers_after_tool_result() -> None:
    """把工具结果回填后模型能据此作答，且不再发起调用。"""
    client = OpenAIClient(_secrets(), max_retries=2)
    try:
        reply = await client.complete(
            [
                {"role": "user", "content": "北京今天天气怎么样？"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"query": "北京天气"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "search",
                    "content": "1. 北京今天晴，气温 25 度\n   https://weather.example.com/beijing",
                },
            ],
            tools=TOOL_SCHEMA,
        )
    finally:
        await client.aclose()

    assert not reply.tool_calls
    assert "25" in reply.content or "晴" in reply.content


async def test_live_model_auth_error_is_classified(tmp_path) -> None:
    """错误密钥应被归类为鉴权错误，而不是当作可重试故障。"""
    from newsbot.core.errors import AuthError
    from newsbot.core.llm import classify_error

    bad = Secrets(_env_file=None, llm_api_key="sk-invalid", llm_base_url=_secrets().llm_base_url)
    client = OpenAIClient(bad, max_retries=1)
    try:
        with pytest.raises(AuthError):
            await client.complete([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    assert classify_error(AuthError("x")).__name__ == "AuthError"


async def test_live_usage_can_be_recorded(tmp_path) -> None:
    """真实调用的用量可以写入 llm_usage，供成本统计使用。"""
    from sqlalchemy import select

    from newsbot.core.llm import record_usage

    client = OpenAIClient(_secrets(), max_retries=2)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    await db.init()
    try:
        reply = await client.complete([{"role": "user", "content": "回复 ok"}])
        async with db.session() as session:
            await record_usage(session, reply, model=_secrets().llm_model)
            await session.commit()
            row = (await session.execute(select(LlmUsage))).scalar_one()
    finally:
        await client.aclose()
        await db.dispose()

    assert row.model and row.prompt_tokens > 0
