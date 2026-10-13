"""
模块: result.summarize
职责: 事件综述——把同一件事的多条报道写成一两句可读的说明，落进 `story.digest`
依赖: core.config, core.db, core.llm, core.log, core.models
来源: 「综述由模型写、事件页与日报复用」这一设计取自 AIHOT（MIT，KKKKhazix/AIHOT）
      packages/backend/src/industry/*，提示词与过期判定按本项目的实测重写
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.llm import LLM, Usage
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem, ReportItem, Story

logger = get_logger(__name__)

# 一次送进模型多少条报道。**取最早的一条加最新的一条**，中间的不给：
# 读者需要的是"这是什么"与"最新到哪了"，中间过程对综述没有增量（实测给全部报道时
# 模型会把它们按顺序复述一遍，写成流水账）。
MAX_REPORTS_IN_PROMPT = 4
# 每条报道的正文摘要截断长度。综述是**概述**不是全文理解，给太长只是把推理 token 烧掉
BODY_CHARS = 400
# 综述的长度上限（字符）。模型偶尔会写成一整段，截断比任由它污染日报版面好。
# 取 160：两句话的中文大约这么多，足够说清"谁做了什么、有什么影响"
MAX_DIGEST_CHARS = 160
# 低于这个字数的输出视为没写出来（模型返回空正文或只吐了标点）
MIN_DIGEST_CHARS = 8

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SYSTEM = """你在给一份新闻聚合产品编写事件综述。

读者只看到事件标题和这一句综述，然后决定要不要点进去。所以综述要回答"这是什么事"，
而不是复述标题。

要求：
- 一两句话，中文，不超过 80 字。
- 只写材料里明确说过的内容。**不要补充背景知识，不要推测影响，不要下判断。**
- 材料里没有的数字、时间、人名一律不写。宁可少写。
- 同一件事的多条报道说的是同一件事，不要去比较它们之间的措辞差异。
- 直接输出综述正文，不要任何前缀、编号、引号或"该事件"之类的套话。"""


@dataclass
class Material:
    """一条报道的可用素材。"""

    title: str
    body: str = ""
    source: str = ""
    observed_at: datetime | None = None


@dataclass
class Pending:
    """待写综述的事件，以及**其余事件为什么没被选中**。

    为什么要把跳过原因带出来：只有 `items` 的话，日志会打成「综述 0 条（未过期跳过 0,
    独家免写 0）」——读起来像"跳过逻辑根本没生效"，而实际上 143 个事件全被跳过了。
    计数器的意义就在于把"没做事"和"没什么可做"分开。
    """

    items: list[tuple[Story, list[Any], int]] = field(default_factory=list)
    single: int = 0
    fresh: int = 0


@dataclass
class SummaryResult:
    """一次综述刷新的结果。"""

    written: int = 0
    skipped_fresh: int = 0
    skipped_single: int = 0
    failed: int = 0
    usage: list[Usage] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"综述 {self.written} 条（未过期跳过 {self.skipped_fresh}，"
            f"独家免写 {self.skipped_single}，失败 {self.failed}）"
        )


def build_prompt(title: str, materials: list[Material]) -> list[dict[str, str]]:
    """综述提示词。规格在前、材料在中、格式要求在最末尾。

    与打分同一套约定：格式要求放末尾（放前面时模型会先写一段分析，而推理 token 与
    正文共用额度，正文容易被挤空）。
    """
    blocks: list[str] = []
    for index, item in enumerate(materials, start=1):
        body = item.body.strip()
        if len(body) > BODY_CHARS:
            body = body[:BODY_CHARS] + "…"
        when = f"{item.observed_at:%Y-%m-%d}" if item.observed_at else "日期未知"
        source = item.source or "来源未标注"
        lines = [f"[{index}] {item.title}", f"来源：{source}　日期：{when}"]
        if body:
            lines.append(f"内容：{body}")
        blocks.append("\n".join(lines))
    joined = "\n\n".join(blocks)
    return [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": f"事件标题：{title}\n\n以下是关于这件事的报道：\n{joined}\n\n请写出这个事件的综述。",
        },
    ]


def parse_digest(text: str) -> str:
    """清洗模型输出。取不到可用内容返回空串（调用方按"没写成"处理）。

    清洗的必要性来自实测：即使提示词明确要求"直接输出正文"，模型仍偶尔带上
    `综述：` 前缀或把自己写的 1./2. 编号一起吐出来，这些混进日报版面很难看。
    只去掉**最外层**的包裹，不去掉文中的标点或数字（那是内容）。
    """
    value = (text or "").strip()
    if not value:
        return ""
    # 去掉常见的行首标签（可能带引号或方括号）
    for prefix in ("综述：", "综述:", "摘要：", "摘要:", "事件综述：", "事件综述:"):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    # 整句被引号包住时脱掉引号
    for left, right in (("「", "」"), ("“", "”"), ('"', '"'), ("'", "'")):
        if value.startswith(left) and value.endswith(right) and len(value) > len(left) + len(right):
            value = value[len(left) : -len(right)].strip()
            break
    # 换行折成一个空格：日报是逐行版面，综述内部的换行会打乱缩进
    value = " ".join(value.split())
    if len(value) > MAX_DIGEST_CHARS:
        value = value[:MAX_DIGEST_CHARS].rstrip("，,、;；。 ") + "…"
    return value if len(value) >= MIN_DIGEST_CHARS else ""


def materials_of(records: list[tuple[ReportItem, NewsItem]]) -> list[Material]:
    """从事件的报道里挑出送进模型的素材：**最早一条 + 最新一条**（不足则全给）。

    为什么不给全部：实测给 5 条以上时模型倾向于按顺序复述，把综述写成流水账；
    而"这是什么"由最早那条定调，"最新到哪了"由最新那条定，中间过程没有增量。
    """
    if not records:
        return []
    # `_as_utc` 对 None 返回 None，排序时统一兜到 datetime.min，避免 mypy 与
    # 运行时的 None 比较问题（observed_at 有数据库默认值，正常不会是 None）
    ordered = sorted(records, key=lambda row: _as_utc(row[0].observed_at) or _EPOCH)
    picked = ordered if len(ordered) <= MAX_REPORTS_IN_PROMPT else [ordered[0], *ordered[-2:]]
    seen: set[str] = set()
    materials: list[Material] = []
    for report, item in picked:
        title = (item.title or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        materials.append(
            Material(
                title=title,
                body=(item.summary or "").strip(),
                source=str(report.participant or item.source or ""),
                observed_at=_as_utc(report.observed_at),
            )
        )
    return materials


@dataclass
class Summarizer:
    """给事件写综述。只为**多来源**事件写，见 `pending`。"""

    llm: LLM
    config: EditorialSection = field(default_factory=EditorialSection)

    @classmethod
    def from_config(cls, llm: LLM, config: EditorialSection) -> Summarizer:
        return cls(llm=llm, config=config)

    async def write(self, title: str, materials: list[Material]) -> tuple[str, Usage | None]:
        """写一条综述；失败返回空串（调用方跳过这个事件，不影响其余）。"""
        reply = await asyncio.wait_for(
            self.llm.complete(build_prompt(title, materials), max_tokens=self.config.summary_max_tokens),
            self.config.summary_timeout_seconds,
        )
        used = reply.usage() if (reply.prompt_tokens or reply.completion_tokens) else None
        return parse_digest(reply.content), used

    async def refresh(self, db: Database, *, limit: int | None = None) -> SummaryResult:
        """给"该写或该重写"的事件补综述。逐个处理，单个失败不影响其余。"""
        result = SummaryResult()
        pending = await self.pending(db, limit=limit)
        result.skipped_single = pending.single
        result.skipped_fresh = pending.fresh
        for story, records, _ in pending.items:
            materials = materials_of(records)
            if not materials:
                result.failed += 1
                logger.warning("事件 #%d 没有可用素材，跳过综述", story.id)
                continue
            try:
                text, used = await self.write(story.title, materials)
            except Exception as exc:  # 单条失败不拖垮整批
                result.failed += 1
                logger.warning("事件 #%d 综述失败: %s: %s", story.id, type(exc).__name__, exc)
                continue
            if used is not None:
                result.usage.append(used)
            if not text:
                result.failed += 1
                logger.warning("事件 #%d 的综述为空（模型返回空正文或只有标点）", story.id)
                continue
            await save_digest(db, story.id, text, len(records))
            result.written += 1
        logger.info("事件综述完成：%s", result.line())
        return result

    async def pending(self, db: Database, *, limit: int | None = None) -> Pending:
        """挑出该写综述的事件。返回待办清单 + 各类跳过计数。

        **只为多来源事件写**：独家事件的一条报道就是它全部的内容，标题已经把事说清楚了
        （实测这类条目的"综述"就是把标题重复一遍，纯噪声）。真正的增益只出现在多家报道时
        ——两类来源的说法不同，读者需要一句话把它们统起来。

        **过期判定**：`digest_reports` 记录了上次写综述时已有多少条报道，新报道到来后
        `len(records) > digest_reports` 才重写。没有这个数就只剩两种坏选择：永不更新
        （长期事件永远描述着最初那两家），或每次刷新都重写（白花钱，而且同一件事的措辞
        每半小时变一次）。
        """
        async with db.session() as session:
            rows = (
                await session.execute(
                    select(ReportItem, NewsItem).join(NewsItem, NewsItem.id == ReportItem.news_item_id)
                )
            ).all()
        grouped: dict[int, list[Any]] = {}
        for report, item in rows:
            grouped.setdefault(report.story_id, []).append((report, item))

        async with db.session() as session:
            stories = {story.id: story for story in (await session.scalars(select(Story))).all()}

        pending = Pending()
        for story_id, records in sorted(grouped.items()):
            story = stories.get(story_id)
            if story is None:
                continue
            participants = {str(report.participant or "") for report, _ in records}
            participants.discard("")
            if len(participants) < 2:
                pending.single += 1
                continue
            covered = int(story.digest_reports or 0)
            if story.digest and covered >= len(records):
                pending.fresh += 1
                continue
            pending.items.append((story, records, covered))
        chosen = pending.items
        if limit is not None and len(chosen) > limit:
            # 按"漏得最多"排序：攒了 5 条新报道的事件比只多 1 条的更该先写
            chosen.sort(key=lambda row: len(row[1]) - row[2], reverse=True)
            logger.warning("待写综述 %d 个事件超过上限 %d，只处理缺口最大的前 %d 个", len(chosen), limit, limit)
            pending.items = chosen[:limit]
        return pending


async def save_digest(db: Database, story_id: int, text: str, reports: int) -> None:
    """把综述写回事件。`reports` 一并记下，作为"是否过期"的依据。"""
    async with db.session() as session:
        story = await session.get(Story, story_id)
        if story is None:  # 并发下事件可能刚被 --reset 清掉
            return
        story.digest = text
        story.digest_reports = reports
        await session.commit()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
