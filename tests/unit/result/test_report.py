"""
模块: tests.unit.result.test_report
职责: 校验成稿的正文截断、来源编号、Markdown 转纯文本与摘要兜底
依赖: result.report
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.fakes.llm import FakeLLM, reply

from newsbot.core.llm import LLMReply
from newsbot.core.models import Task
from newsbot.result.report import ReportBuilder, add_source_list, format_for_platform, to_plain


@dataclass
class Findings:
    answer: str = "结论 [1]"
    sources: list[dict[str, str]] = field(default_factory=list)


def _task(query: str = "今天 AI 新闻") -> Task:
    return Task(kind="chat", query=query)


def test_add_source_list_numbers_sources() -> None:
    text = add_source_list("结论 [1]", [{"title": "标题", "url": "https://a.example/1"}])
    assert text.startswith("结论 [1]\n\n来源：")
    assert "1. 标题 https://a.example/1" in text


def test_add_source_list_skips_when_empty() -> None:
    assert add_source_list("结论", []) == "结论"


def test_to_plain_strips_markdown() -> None:
    plain = to_plain("# 标题\n\n**要点**：`代码`\n\n[链接](https://a.example)")
    assert "#" not in plain
    assert "**" not in plain
    assert "`" not in plain
    assert "链接：https://a.example" in plain


def test_format_for_platform_plain_only_for_weixin() -> None:
    assert "**粗体**" not in format_for_platform("**粗体**", "weixin")
    assert "**粗体**" in format_for_platform("**粗体**", "qqbot")
    assert "**粗体**" in format_for_platform("**粗体**", None)


async def test_build_uses_findings_answer_and_sources() -> None:
    findings = Findings(answer="本周三个变化 [1]", sources=[{"title": "标题", "url": "https://a.example/1"}])
    built = await ReportBuilder().build(_task(), findings)
    assert built.content.startswith("本周三个变化 [1]")
    assert "来源：" in built.content
    assert built.sources == [{"title": "标题", "url": "https://a.example/1"}]


async def test_build_truncates_long_content() -> None:
    findings = Findings(answer="长" * 5000, sources=[])
    built = await ReportBuilder(max_chars=200).build(_task(), findings)
    assert "内容过长已截断" in built.content
    assert len(built.content) < 300


async def test_empty_answer_without_llm_uses_placeholder() -> None:
    findings = Findings(answer="", sources=[{"title": "标题", "url": "https://a.example/1"}])
    built = await ReportBuilder().build(_task(), findings)
    assert "未生成结论" in built.content


async def test_empty_answer_and_no_sources_says_no_info() -> None:
    built = await ReportBuilder().build(_task(), Findings(answer="", sources=[]))
    assert "没有采集到可用信息" in built.content


async def test_empty_answer_with_llm_summarizes() -> None:
    llm = FakeLLM(replies=[reply("摘要结论 [1]")])
    findings = Findings(answer="", sources=[{"title": "标题", "url": "https://a.example/1"}])
    built = await ReportBuilder(llm).build(_task(), findings)
    assert built.content.startswith("摘要结论 [1]")
    assert llm.calls[0][-1]["role"] == "user"
    assert "今天 AI 新闻" in llm.calls[0][-1]["content"]


async def test_summarizer_failure_falls_back_to_placeholder() -> None:
    class BoomLLM:
        async def complete(self, *args: object, **kwargs: object) -> LLMReply:
            raise RuntimeError("模型不可用")

        async def aclose(self) -> None:
            return None

    findings = Findings(answer="", sources=[{"title": "标题", "url": "https://a.example/1"}])
    built = await ReportBuilder(BoomLLM()).build(_task(), findings)
    assert "未生成结论" in built.content
