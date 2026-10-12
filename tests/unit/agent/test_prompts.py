"""
模块: tests.unit.agent.test_prompts
职责: 校验消息装配——系统约束、历史裁剪、空内容过滤
依赖: agent.prompts
"""

from __future__ import annotations

from datetime import UTC, datetime

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


def test_system_prompt_asks_to_merge_same_story_sources() -> None:
    """正文里同一件事只能写一条：去重管不到"已经写进正文的重复"。"""
    assert "同一件事只写一条" in SYSTEM_PROMPT
    assert "编号并列引用" in SYSTEM_PROMPT


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


# ── 时间意识 ──
# 实测缺陷：提示词里原本没有任何"今天是几号"，于是日报把 10-08 的旧稿当成"最新进展"、
# 把 10-10 写成"最近一天"（报告实际生成于 10-11）。模型没有这个事实就无法执行
# "最近一天""最新"这类要求。


def test_system_message_carries_current_time_and_timezone() -> None:
    moment = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
    system = build_messages("今天有什么新闻", now=moment, timezone="Asia/Shanghai")[0]["content"]
    assert "2026-10-12 22:30" in system, "应按指定时区渲染（UTC 14:30 ＝ 北京 22:30）"
    assert "Asia/Shanghai" in system
    assert "周一" in system, "2026-10-12 是周一"


def test_time_rule_makes_dates_the_only_basis_for_freshness() -> None:
    system = build_messages("问", now=datetime(2026, 10, 12, tzinfo=UTC))[0]["content"]
    assert "唯一依据" in system
    assert "日期未知" in system, "没有日期的条目要能被识别为无法判断新旧"
    assert "不要凭印象推断" in system


def test_time_is_rendered_per_call_not_frozen() -> None:
    """时间必须每次调用重新取：进程要挂机跑很多天，写成模块常量会一直停在启动那天。"""
    first = build_messages("问", now=datetime(2026, 10, 12, 8, 0, tzinfo=UTC))[0]["content"]
    second = build_messages("问", now=datetime(2026, 10, 20, 8, 0, tzinfo=UTC))[0]["content"]
    assert first != second
    assert "2026-10-12" in first and "2026-10-20" in second


def test_naive_time_is_treated_as_local_zone() -> None:
    system = build_messages("问", now=datetime(2026, 10, 12, 9, 0), timezone="Asia/Shanghai")[0]["content"]
    assert "2026-10-12 09:00" in system, "无时区的时间应按时区解释，不要再平移一次"


def test_bad_timezone_falls_back_instead_of_crashing() -> None:
    """时区名写错不该让整个任务起不来，退回本机时区继续。"""
    system = build_messages("问", now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC), timezone="Not/AZone")[0]["content"]
    assert "2026-10-12" in system


def test_age_gap_survives_long_running_process() -> None:
    """挂机模式下每次运行都要看到当天日期，而不是上次运行留下的。"""
    runner_time = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
    system = build_messages("问", now=runner_time)[0]["content"]
    assert "2026-10-12" in system
