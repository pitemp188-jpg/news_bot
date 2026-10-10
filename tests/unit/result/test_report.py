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
    assert "S1. 标题 https://a.example/1" in text


def test_add_source_list_skips_when_empty() -> None:
    assert add_source_list("结论", []) == "结论"


def test_add_source_list_uses_label_embedded_in_source() -> None:
    # 编号存在来源里：上游剔除几条来源后，剩下的编号不会整体前移
    sources = [
        {"title": "标题", "url": "https://a.example/1", "label": "S9"},
        {"title": "标题2", "url": "https://a.example/2", "label": "S14"},
    ]
    text = add_source_list("结论 [S14]", sources)
    assert "S9. 标题 https://a.example/1" in text
    assert "S14. 标题2 https://a.example/2" in text
    assert "S1. " not in text
    assert "S2. " not in text


def test_add_source_list_falls_back_to_position() -> None:
    # 没有 label 的来源（例如别处构造的）按位置编号，不能空着
    text = add_source_list("结论", [{"title": "标题", "url": "https://a.example/1"}])
    assert "S1. 标题 https://a.example/1" in text


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
    findings = Findings(
        answer="本周三个变化 [S1]", sources=[{"title": "标题", "url": "https://a.example/1", "label": "S1"}]
    )
    built = await ReportBuilder().build(_task(), findings)
    assert built.content.startswith("本周三个变化 [S1]")
    assert "S1. 标题 https://a.example/1" in built.content
    assert built.sources == [{"title": "标题", "url": "https://a.example/1", "label": "S1"}]


async def test_build_truncates_long_content() -> None:
    findings = Findings(answer="长" * 5000, sources=[])
    built = await ReportBuilder(max_chars=200).build(_task(), findings)
    assert "内容过长已截断" in built.content
    # 正文至少要留 MIN_ANSWER_CHARS，不能因为 max_chars 设得小而把结论砍光
    assert len(built.content) < 900
    assert built.content.index("…（内容过长已截断）") >= 600


async def test_build_lists_only_cited_sources() -> None:
    # 读者要的是"这句话出自哪"；把检索路过的无关条目全列出来既无助于核对又占版面
    sources = [
        {"title": "被引用的", "url": "https://a.example/1", "label": "S1"},
        {"title": "路过的", "url": "https://a.example/2", "label": "S2"},
    ]
    findings = Findings(answer="结论 [S1]", sources=sources)
    built = await ReportBuilder().build(_task(), findings)
    assert "S1. 被引用的" in built.content
    assert "S2. 路过的" not in built.content
    assert "另有 1 条" in built.content
    # 落库的是全部来源：去重要用、资讯库要用
    assert built.sources == sources


async def test_build_keeps_all_sources_when_answer_cites_nothing() -> None:
    # 模型偶尔会忘了写编号，此时宁可多列也不能把来源全丢掉
    sources = [{"title": "标题", "url": "https://a.example/1", "label": "S1"}]
    findings = Findings(answer="没有编号的结论", sources=sources)
    built = await ReportBuilder().build(_task(), findings)
    assert "S1. 标题" in built.content


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


def test_add_source_list_caps_listing_and_says_how_many_dropped() -> None:
    # 列出 20+ 条来源没人看，还会把正文挤掉（实测 22 条来源导致正文只剩"已截断"）
    sources = [{"title": f"标题{i}", "url": f"https://a.example/{i}", "label": f"S{i}"} for i in range(1, 16)]
    text = add_source_list("结论", sources)
    assert "S1. 标题1" in text and "S12. 标题12" in text
    assert "S13. 标题13" not in text
    assert "另有 3 条来源未列出" in text
