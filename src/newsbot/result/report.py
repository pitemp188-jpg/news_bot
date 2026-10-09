"""
模块: result.report
职责: 成稿——汇总结论文本、附来源编号、按平台转纯文本或保留 Markdown
依赖: core.llm, core.log, core.models
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from newsbot.core.llm import LLM
from newsbot.core.log import get_logger
from newsbot.core.models import Task

logger = get_logger(__name__)

MAX_CHARS = 2000
PLAIN_PLATFORMS = {"weixin"}

_INLINE_MARKS = re.compile(r"[*_`]+")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass
class BuiltReport:
    """成稿：正文 + 来源列表。"""

    content: str
    sources: list[dict[str, str]] = field(default_factory=list)


def to_plain(text: str) -> str:
    """去掉 Markdown 标记，供不支持 Markdown 的平台使用。"""
    plain = _LINK.sub(r"\1：\2", text)
    plain = _HEADING.sub("", plain)
    plain = _INLINE_MARKS.sub("", plain)
    return re.sub(r"\n{3,}", "\n\n", plain).strip()


def add_source_list(text: str, sources: list[dict[str, str]], labels: list[str] | None = None) -> str:
    """在正文末尾追加编号来源；编号与模型在工具输出里看到的一致（S1、S2…）。

    过去这里自己从 1 重新编号并截断到 10 条，导致正文的 [1] 与列表第 1 条
    根本不是同一篇（实测日报正文写"出自爱范儿早报"，列表第 1 条却是 InfoQ
    的另一篇），被引用的来源还可能因截断直接消失。现在改用全局编号且不截断。
    """
    if not sources:
        return text
    marks = list(labels or []) or [f"S{index}" for index in range(1, len(sources) + 1)]
    marks += [f"S{index}" for index in range(len(marks) + 1, len(sources) + 1)]
    lines = []
    for index, item in enumerate(sources):
        mark = marks[index] if index < len(marks) else f"S{index + 1}"
        title = item.get("title") or item.get("url") or ""
        lines.append(f"{mark}. {title} {item.get('url', '')}".rstrip())
    return f"{text}\n\n来源：\n" + "\n".join(lines)


class ReportBuilder:
    """把 Agent 采集结果整理成可直接投递的成稿。"""

    def __init__(self, llm: LLM | None = None, *, max_chars: int = MAX_CHARS) -> None:
        self._llm = llm
        self._max_chars = max_chars

    async def build(self, task: Task, findings: Any) -> BuiltReport:
        answer = str(getattr(findings, "answer", "") or "").strip()
        sources = [dict(item) for item in (getattr(findings, "sources", None) or [])]
        labels = [str(item) for item in (getattr(findings, "labels", None) or [])]
        if not answer:
            answer = await self._summarize(task, sources, labels)
        content = add_source_list(answer, sources, labels)
        if len(content) > self._max_chars:
            # 只能截正文，不能把来源列表截掉——否则被引用的来源会消失
            sources_block = add_source_list("", sources, labels)
            room = max(0, self._max_chars - len(sources_block))
            answer = answer[:room] + "\n…（内容过长已截断）"
            content = add_source_list(answer, sources, labels)
        return BuiltReport(content=content, sources=sources)

    async def _summarize(self, task: Task, sources: list[dict[str, str]], labels: list[str] | None = None) -> str:
        if not sources:
            return "本次没有采集到可用信息，请稍后重试或换一种问法。"
        if self._llm is None:
            return "本次未生成结论，以下是可参考的来源："
        marks = list(labels or []) or [f"S{index}" for index in range(1, len(sources) + 1)]
        marks += [f"S{index}" for index in range(len(marks) + 1, len(sources) + 1)]
        listing = "\n".join(
            f"{marks[index]}. {item.get('title', '')} {item.get('url', '')}" for index, item in enumerate(sources)
        )
        try:
            reply = await self._llm.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是资讯编辑，请根据给定来源用中文写出 200 字以内的要点摘要，"
                            "每条要点后用来源列表里的编号标注（例如 [S1]），不要编造来源之外的信息。"
                        ),
                    },
                    {"role": "user", "content": f"用户问题：{task.query}\n\n可用来源：\n{listing}"},
                ]
            )
            return reply.content.strip()
        except Exception as exc:  # 摘要失败时仍要保证有可投递内容
            logger.warning("生成摘要失败: %s", exc)
            return "本次未生成结论，以下是可参考的来源："


def format_for_platform(content: str, platform: str | None) -> str:
    """按平台能力决定是否保留 Markdown。"""
    return to_plain(content) if (platform or "").lower() in PLAIN_PLATFORMS else content
