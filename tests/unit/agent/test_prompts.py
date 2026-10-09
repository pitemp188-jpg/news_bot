"""
模块: tests.unit.agent.test_prompts
职责: 校验消息装配——系统约束、历史裁剪、空内容过滤
依赖: agent.prompts
"""

from __future__ import annotations

from newsbot.agent.prompts import SYSTEM_PROMPT, build_messages


def test_system_prompt_states_untrusted_web() -> None:
    assert "不可信" in SYSTEM_PROMPT
    assert "news_db" in SYSTEM_PROMPT


def test_messages_start_with_system_and_end_with_query() -> None:
    messages = build_messages("今天有什么新闻")
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "今天有什么新闻"}
    assert len(messages) == 2


def test_history_is_kept_in_order() -> None:
    history = [
        {"role": "user", "content": "上一问"},
        {"role": "assistant", "content": "上一答"},
    ]
    messages = build_messages("追问", history)
    assert [m["content"] for m in messages[1:3]] == ["上一问", "上一答"]


def test_invalid_and_blank_turns_are_skipped() -> None:
    history = [
        {"role": "tool", "content": "内部消息"},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": None},
        {"content": "缺少角色"},
    ]
    messages = build_messages("问", history)
    assert len(messages) == 2


def test_query_is_stripped() -> None:
    assert build_messages("  空格问题  ")[-1]["content"] == "空格问题"
