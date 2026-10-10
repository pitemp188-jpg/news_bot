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


def test_system_prompt_forbids_inventing_urls() -> None:
    # 实测模型会凭空抓取记忆中的网站（如 theverge），必须显式禁止
    assert "禁止凭印象拼凑或推测网址" in SYSTEM_PROMPT
    assert "只能引用工具返回过的网址" in SYSTEM_PROMPT
    assert "不要编造" in SYSTEM_PROMPT


# ── harness 化：提示词只给目标与底线，流程由模型自己决定 ──
# 曾经把 1/2/3/4 步骤和"搜索最多 2～3 次"写进提示词，等于用模板替模型做规划，
# 结果是模型失去自主性、也失去"检索词没写好"这个反馈回路的学习机会。


def test_system_prompt_does_not_prescribe_a_procedure() -> None:
    # 旧版把 1/2/3/4 步骤与"搜索最多 2～3 次"写进提示词，等于用模板替模型做规划
    assert "工作方式" not in SYSTEM_PROMPT, "不该再有步骤清单"
    assert "搜索最多" not in SYSTEM_PROMPT
    assert "通常只抓" not in SYSTEM_PROMPT
    assert "由你根据当前掌握的信息判断" in SYSTEM_PROMPT, "策略归属要明确交给模型"


def test_system_prompt_asks_model_to_state_its_plan() -> None:
    # 规划必须可观测，否则只能在日志里事后推断规划失败（实测踩过）
    assert "想清楚" in SYSTEM_PROMPT
    assert "打算查什么" in SYSTEM_PROMPT


def test_system_prompt_makes_search_feedback_actionable() -> None:
    # 工具会如实反馈哪些检索词没命中，提示词要告诉模型那是让它改写检索词
    assert "检索词由你负责提炼" in SYSTEM_PROMPT
    assert "没有命中" in SYSTEM_PROMPT


def test_system_prompt_leaves_budget_judgement_to_model() -> None:
    assert "硬上限" in SYSTEM_PROMPT
    assert "收手" in SYSTEM_PROMPT


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
