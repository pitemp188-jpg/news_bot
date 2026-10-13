"""
模块: result.summarize
职责: 事件综述——把同一件事的多条报道写成一两句可读的说明，落进 `story.digest`
依赖: core.config, core.db, core.llm, core.log, core.models
来源: 「综述由模型写、事件页与日报复用」这一设计取自 AIHOT（MIT，KKKKhazix/AIHOT）
      packages/backend/src/industry/*，提示词与过期判定按本项目的实测重写
"""

from __future__ import annotations

import asyncio
import re
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
# 事件标题的长度上限。事件标题此前直接用**第一条报道的标题**，而英文媒体的标题动辄
# 一两百字符（实测「'Unprecedented' Cyberattack cripples supermarket fridges across
# Europe as hackers activate automatic defrosting — delivery and store prices also
# impacted by incident」），在聊天里会折成一片、在事件列表里也读不出重点。既然综述
# 那次调用已经在读同一份素材，让它顺手给一个中文短标题，**不额外花钱**。
MAX_TITLE_CHARS = 40
# 低于这个字数的输出视为没写出来（模型返回空正文或只吐了标点）
MIN_DIGEST_CHARS = 8
# 低于这个长度的标题视为没写出来（模型返回空行或只吐了标点）
MIN_TITLE_CHARS = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SYSTEM = """你在给一份新闻聚合产品编写事件标题与事件综述。

读者在列表里先看到标题，决定要不要点进去；进去之后读到综述。所以：
- 标题要说清"谁做了什么"，不超过 20 个字，用中文。
- 综述一到两句话，补充标题没说到的关键信息，不超过 80 字。

要求：
- 只写材料里明确说过的内容。**不要补充背景知识，不要推测影响，不要下判断。**
- 材料里没有的数字、时间、人名一律不写。宁可少写。
- 同一件事的多条报道说的是同一件事，不要去比较它们之间的措辞差异。
- 按下面的格式输出**两行**，不要任何其他内容：
标题：<中文短标题>
综述：<一到两句话>

示例：
标题：Cloudflare 收购 Deno 项目
综述：Cloudflare 收购 Deno 公司，Deno 项目停止开发，收购旨在改进 Cloudflare Workers 编程模型。"""

_TITLE_ONLY_SYSTEM = """你在给一份新闻聚合产品编写事件标题。

读者在列表里先看到标题，决定要不要点进去。所以标题要说清"谁做了什么"，
不超过 20 个字，用中文——原文标题若是英文，翻成中文并压缩到能一眼看懂。

要求：
- 只写材料里明确说过的内容。**不要补充背景知识，不要推测影响，不要下判断。**
- 材料里没有的数字、时间、人名一律不写。宁可少写。
- 只输出**一行**，不要任何其他内容：
标题：<中文短标题>"""


@dataclass
class Material:
    """一条报道的可用素材。"""

    title: str
    body: str = ""
    source: str = ""
    observed_at: datetime | None = None


@dataclass
class Target:
    """一个待处理的事件：事件本身、它的报道、需要做什么。"""

    story: Story
    records: list[Any]
    covered: int
    # 是否还需要正文综述。多来源事件需要（各家的说法不同，读者要一句话统起来）；
    # 单来源事件不需要——它唯一那条报道就是全部内容，写"综述"只会把标题重复一遍
    with_digest: bool = True


@dataclass
class Pending:
    """待处理的事件，以及**其余事件为什么没被选中**。

    为什么要把跳过原因带出来：只有 `items` 的话，日志会打成「综述 0 条（未过期跳过 0)」
    ——读起来像"跳过逻辑根本没生效"，而实际上 143 个事件全被跳过了。计数器的意义
    就在于把"没做事"和"没什么可做"分开。
    """

    items: list[Target] = field(default_factory=list)
    fresh: int = 0


@dataclass
class SummaryResult:
    """一次综述刷新的结果。"""

    written: int = 0
    # 顺带改了标题的事件数。标题由模型按同一份素材写成中文短句，此前它是**第一条报道
    # 的原标题**（英文媒体动辄一两百字符），在聊天里会折成一片
    retitled: int = 0
    # 只改了标题、没写正文的事件数（单来源事件走这条路）
    title_only: int = 0
    skipped_fresh: int = 0
    failed: int = 0
    usage: list[Usage] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"改标题 {self.retitled} 条（其中带综述 {self.written} 条、只改标题 {self.title_only} 条）"
            f"，未过期跳过 {self.skipped_fresh}，失败 {self.failed}"
        )


def build_prompt(title: str, materials: list[Material], *, with_digest: bool = True) -> list[dict[str, str]]:
    """综述提示词。规格在前、材料在中、格式要求在最末尾。

    与打分同一套约定：格式要求放末尾（放前面时模型会先写一段分析，而推理 token 与
    正文共用额度，正文容易被挤空）

    `with_digest=False` 用于**单来源**事件：它唯一那条报道就是全部内容，"综述"只会把
    标题重复一遍，所以只要一个中文短标题（原文标题常是英文长句，在聊天里折成一片）。
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
    tail = "请按格式输出这个事件的标题与综述。" if with_digest else "请按格式输出这个事件的标题。"
    return [
        {"role": "system", "content": _SYSTEM if with_digest else _TITLE_ONLY_SYSTEM},
        {"role": "user", "content": f"事件的原标题：{title}\n\n以下是关于这件事的报道：\n{joined}\n\n{tail}"},
    ]


_TITLE_LINE = re.compile(r"^\s*(?:\*\*)?标题(?:\*\*)?\s*[:：]\s*(.+)$", re.MULTILINE)
_DIGEST_LINE = re.compile(r"^\s*(?:\*\*)?综述(?:\*\*)?\s*[:：]\s*(.+)$", re.MULTILINE)


def parse_title(text: str) -> str:
    """从模型输出里取出「标题：…」那一行；取不到返回空串。

    取不到就**返回空串**而不是编一个：调用方会保留原标题。这里必须"要么给出可用标题
    要么什么都不给"——用原标题兜底会让"模型没写"被伪装成"模型写的标题就是原标题"。
    """
    match = _TITLE_LINE.search(text or "")
    if match is None:
        return ""
    value = " ".join(match.group(1).split()).strip("「」“”\"'")
    if not MIN_TITLE_CHARS <= len(value) <= MAX_TITLE_CHARS:
        return ""
    return value


def parse_digest(text: str) -> str:
    """清洗模型输出。取不到可用内容返回空串（调用方按"没写成"处理）。

    清洗的必要性来自实测：即使提示词明确要求"直接输出正文"，模型仍偶尔带上
    `综述：` 前缀或把自己写的 1./2. 编号一起吐出来，这些混进日报版面很难看。
    只去掉**最外层**的包裹，不去掉文中的标点或数字（那是内容）。

    **先按行取「综述：」那一行**：标题与综述是同一次调用的两行输出，不先切出来就会
    把「标题：…」也当成正文的一部分写进日报。
    """
    value = (text or "").strip()
    if not value:
        return ""
    matched = _DIGEST_LINE.search(value)
    if matched is not None:
        value = matched.group(1).strip()
    elif _TITLE_LINE.search(value) is not None:
        # 有「标题：」行却没有「综述：」行，说明模型**没给正文**。必须返回空串：否则
        # 整段「标题：xxx」会被当正文收下（长度通常够），于是"没写正文"被伪装成写好了，
        # 而日报正是靠"综述为空"回退到标题列表的。
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

    async def write(
        self, title: str, materials: list[Material], *, with_digest: bool = True
    ) -> tuple[str, str, Usage | None]:
        """写事件标题（以及需要时的综述）；失败返回空串，调用方跳过这个事件。"""
        reply = await asyncio.wait_for(
            self.llm.complete(
                build_prompt(title, materials, with_digest=with_digest), max_tokens=self.config.summary_max_tokens
            ),
            self.config.summary_timeout_seconds,
        )
        used = reply.usage() if (reply.prompt_tokens or reply.completion_tokens) else None
        return parse_title(reply.content), parse_digest(reply.content), used

    async def refresh(self, db: Database, *, limit: int | None = None) -> SummaryResult:
        """给"该写或该重写"的事件补标题与综述。逐个处理，单个失败不影响其余。"""
        result = SummaryResult()
        pending = await self.pending(db, limit=limit)
        result.skipped_fresh = pending.fresh
        for target in pending.items:
            story = target.story
            materials = materials_of(target.records)
            if not materials:
                result.failed += 1
                logger.warning("事件 #%d 没有可用素材，跳过", story.id)
                continue
            try:
                new_title, text, used = await self.write(story.title, materials, with_digest=target.with_digest)
            except Exception as exc:  # 单条失败不拖垮整批
                result.failed += 1
                logger.warning("事件 #%d 处理失败: %s: %s", story.id, type(exc).__name__, exc)
                continue
            if used is not None:
                result.usage.append(used)
            # 单来源事件只要标题：正文为空是**预期**，不算失败
            if target.with_digest and not text:
                result.failed += 1
                logger.warning("事件 #%d 的综述为空（模型返回空正文或只有标点）", story.id)
                continue
            if not new_title:
                result.failed += 1
                logger.warning("事件 #%d 没有拿到可用标题", story.id)
                continue
            if new_title != story.title:
                result.retitled += 1
            # 落库放在最后：上面任何一条失败都 `continue`，不会写半成品
            await save_digest(db, story.id, new_title, text, len(target.records))
            if target.with_digest:
                result.written += 1
            else:
                result.title_only += 1
        logger.info("事件综述完成：%s", result.line())
        return result

    async def pending(self, db: Database, *, limit: int | None = None) -> Pending:
        """挑出该处理的事件。返回待办清单 + 过期跳过计数。

        **所有事件都要标题**：此前事件标题是**第一条报道的原标题**，而英文媒体的标题
        动辄一两百字符（实测「'Unprecedented' Cyberattack cripples supermarket fridges
        across Europe as hackers activate automatic defrosting — delivery and store
        prices also impacted by incident」），在聊天里折成一片、在事件列表里读不出重点。
        标题由模型写成中文短句。

        **正文综述只给多来源事件**：独家事件唯一那条报道就是全部内容，写"综述"只会把
        标题重复一遍（纯噪声）。真正的增益只在多家报道时出现——各家的说法不同，读者
        需要一句话把它们统起来。所以单来源事件走 `with_digest=False`。

        **过期判定**：`digest_reports` 记录了上次处理时已有多少条报道，新报道到来后
        `len(records) > digest_reports` 才重做。没有这个数就只剩两种坏选择：永不更新
        （长期事件永远描述着最初那两家），或每次刷新都重做（白花钱，而且同一件事的措辞
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
            # 标题已改过、且没有新报道 → 跳过。判据是 `digest_reports` 而不是 `digest`：
            # 单来源事件的正文本就是空的，用 `digest` 判会每次都重做
            covered = int(story.digest_reports or 0)
            if covered >= len(records):
                pending.fresh += 1
                continue
            participants = {str(report.participant or "") for report, _ in records}
            participants.discard("")
            pending.items.append(
                Target(story=story, records=records, covered=covered, with_digest=len(participants) >= 2)
            )
        if limit is not None and len(pending.items) > limit:
            # 多来源的（要做两件事）优先于单来源的；同为多来源时按"漏得最多"排
            pending.items.sort(key=lambda row: (row.with_digest, len(row.records) - row.covered), reverse=True)
            logger.warning("待处理 %d 个事件超过上限 %d，只处理前 %d 个", len(pending.items), limit, limit)
            pending.items = pending.items[:limit]
        return pending


async def save_digest(db: Database, story_id: int, title: str, text: str, reports: int) -> None:
    """把事件标题与综述写回。`reports` 一并记下，作为"是否过期"的依据。

    标题为空时**保留原标题**（模型没给出可用标题，不该因此把标题清空）。
    """
    async with db.session() as session:
        story = await session.get(Story, story_id)
        if story is None:  # 并发下事件可能刚被 --reset 清掉
            return
        if title:
            story.title = title
        story.digest = text
        story.digest_reports = reports
        await session.commit()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
