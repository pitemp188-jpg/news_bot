"""
模块: result.selection
职责: 精选——同一份标准对每条候选独立打两次分，按信源分级门槛判定入选，评分落库备查
依赖: core.db, core.llm, core.log, core.models, core.config
来源: 两轮打分与分层门槛的流程移植自 AIHOT（MIT，KKKKhazix/AIHOT）
      packages/backend/src/industry/selection.ts，按本项目的数据模型与既有 LLM 客户端重写
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.llm import LLM, Usage
from newsbot.core.log import get_logger
from newsbot.core.models import StoryScore

logger = get_logger(__name__)

# 信源分级。名字沿用行业惯例（T1 是一手，级别越高离一手越远），落库进
# `report_item.tier`，所以是外部可见的取值，不要随意改名
TIER_OFFICIAL = "T1"
TIER_QUASI = "T1_5"
TIER_MEDIA = "T2"
TIERS = (TIER_OFFICIAL, TIER_QUASI, TIER_MEDIA)
DEFAULT_TIER = TIER_MEDIA

# 打分规格。两次打分用的是**同一个字符串**——见 `Selector.score` 的说明
_SYSTEM = "你是资讯主编。请判断下面这条资讯值不值得进当天的日报。\n只有 0～10 这一个整数，没有其他输出格式。"
_RULES = (
    "## 评判标准（对所有来源一视同仁）\n"
    "- 9～10：行业级重要进展，有可核实的事实（发布、融资、事故、数据）\n"
    "- 7～8：有实质信息量，值得读者花时间了解\n"
    "- 5～6：有信息但增量有限，或只是转述别家报道\n"
    "- 3～4：资讯性质，实质性内容稀薄（发布会预告、榜单、导购）\n"
    "- 0～2：推广、水文、与行业无关\n"
    "\n"
    "## 输出\n"
    "只输出一个 0～10 的整数，不要写理由、不要写单位、不要写其他任何字符。"
)

# 只认"整行就是一个数字（可带『分』）"的行。实测模型即使被要求只输出数字，
# 仍可能先写一段分析（推理模型），所以格式要求放在最末尾并做容错解析
_SCORE_LINE = re.compile(r"^\s*(\d{1,2})\s*分?\s*$", re.MULTILINE)
_ANY_NUMBER = re.compile(r"(?<![0-9])(\d{1,2})(?![0-9])")
MAX_SCORE = 10
# 两次打分差距达到多少算"标准不稳定"的兜底值；实际取 config.editorial.unstable_gap
UNSTABLE_GAP_FALLBACK = 3


@dataclass
class Candidate:
    """一条待精选的候选。

    只带打分需要的东西：标题、摘要、来源与时间。**不带正文全文**——一是 token
    成本随条数线性增长，二是正文越长模型越倾向给高分（实测），标题+摘要才是同一把尺子。
    """

    news_item_id: int
    title: str
    text: str = ""
    source: str = ""
    url: str = ""
    tier: str = DEFAULT_TIER

    @property
    def body(self) -> str:
        return f"{self.title}\n{self.text}".strip()


@dataclass
class Score:
    """一条候选的两次打分。

    `mean` 用和的一半而不是先算平均再乘门槛：避免浮点误差让"刚好等于门槛"的条目
    在边界上随机进出。判定写成 `score1 + score2 >= 2 * threshold`。
    """

    news_item_id: int
    score1: int | None = None
    score2: int | None = None
    threshold: int = 0
    reason: str = ""
    # 差距达到多少算不稳定；跟着配置走（Score 自带一份，避免读取时依赖调用方）
    unstable_gap: int = UNSTABLE_GAP_FALLBACK

    @property
    def scored(self) -> bool:
        return self.score1 is not None and self.score2 is not None

    @property
    def total(self) -> int:
        return (self.score1 or 0) + (self.score2 or 0)

    @property
    def mean(self) -> float:
        return self.total / 2

    @property
    def gap(self) -> int:
        return abs((self.score1 or 0) - (self.score2 or 0))

    @property
    def selected(self) -> bool:
        """两次打分之和达到 2 倍门槛即入选。

        未评分的条目**不入选**：宁可漏掉一条，也不能因为模型抽风把整批塞进日报——
        "全选"正是改造前那份满是副本的日报。
        """
        if not self.scored:
            return False
        return self.total >= 2 * self.threshold

    @property
    def unstable(self) -> bool:
        """两次打分差距过大：标准在这条上不稳定，需要人工看。"""
        return self.scored and self.gap >= self.unstable_gap

    def to_row(self) -> StoryScore:
        return StoryScore(
            news_item_id=self.news_item_id,
            score1=self.score1 or 0,
            score2=self.score2 or 0,
            selected=self.selected,
            reason=self.reason or self.describe(),
        )

    def describe(self) -> str:
        if not self.scored:
            return "未评分（模型未返回可用分数）"
        head = f"{self.score1}/{self.score2} 分（门槛 {self.threshold}×2={2 * self.threshold}）"
        return head + ("，两次差距大，建议人工复核" if self.unstable else "")


@dataclass
class Selection:
    """一次精选的结果。"""

    scores: list[Score] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)

    @property
    def selected(self) -> list[Score]:
        return [item for item in self.scores if item.selected]

    @property
    def unstable(self) -> list[Score]:
        return [item for item in self.scores if item.unstable]

    @property
    def unscored(self) -> list[Score]:
        return [item for item in self.scores if not item.scored]


def parse_score(text: str) -> int | None:
    """从模型输出里取出分数；取不到返回 None。

    容错优先：优先认"整行就是一个数字"的行（格式要求放在提示词末尾，所以**取最后
    一个**匹配行），退一步取文中第一个 0～10 的整数。超出范围的整数一律丢掉——
    宁可判为未评分，也不要让 2024 这种年份被当成满分。
    """
    lines = [int(match) for match in _SCORE_LINE.findall(text or "")]
    for value in reversed(lines):
        if 0 <= value <= MAX_SCORE:
            return value
    for match in _ANY_NUMBER.finditer(text or ""):
        value = int(match.group(1))
        if 0 <= value <= MAX_SCORE:
            return value
    return None


def classify_tier(source: str, url: str, *, official: list[str], quasi: list[str]) -> str:
    """按来源域名判信源分级；不在名单里一律 T2。

    用"是否在名单里"而不是猜测：把自己不认识的来源判成官方，等于替读者做了
    "这条更可信"的背书，而背书必须有据可依。
    """
    host = _host(url) or _host(source)
    if not host:
        return DEFAULT_TIER
    if _in_list(host, official):
        return TIER_OFFICIAL
    if _in_list(host, quasi):
        return TIER_QUASI
    return DEFAULT_TIER


def _host(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0].split("@")[-1].split(":", 1)[0]


def _in_list(host: str, domains: list[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in (item.lower().strip() for item in domains))


@dataclass
class Selector:
    """按同一份标准给候选打两次分，再按信源分级门槛判定入选。

    为什么要打两次：一次打分无法区分"这条确实一般"和"这次模型没想清楚"。两次独立
    打分后，**和**用来判定入选（互相印证），**差**用来标记该人工复核的条目
    （`|score1 - score2|` 大说明标准在它身上不稳定，取平均会把这个信号掩盖掉）。

    为什么两次用的是**同一个提示词**：任何"请复核上一遍的结论"式的措辞都会让第二遍
    变成对第一遍的确认（模型倾向于同意），差距信号随之归零。所以第二遍只重复同一份
    规格，**不告诉它这是第二遍，也不带第一遍的分数**。
    """

    llm: LLM
    config: EditorialSection = field(default_factory=EditorialSection)
    unstable_gap: int = UNSTABLE_GAP_FALLBACK

    @classmethod
    def from_config(cls, llm: LLM, config: EditorialSection) -> Selector:
        return cls(llm=llm, config=config, unstable_gap=config.unstable_gap)

    def threshold_of(self, tier: str) -> int:
        """该分级对应的门槛；分级不认识时用 T2 的门槛（更严的那一档）。"""
        thresholds = self.config.tier_thresholds
        if tier in thresholds:
            return thresholds[tier]
        return thresholds.get(DEFAULT_TIER, 8)

    def build_prompt(self, candidate: Candidate) -> list[dict[str, str]]:
        """打分提示词。规格在前、待评内容在中、格式要求在最末尾。

        实测格式要求写在前面时，模型会先写一段分析再给结论（推理 token 与正文共用
        额度，正文容易被挤空）；放到末尾明显收敛。
        """
        body = candidate.body or candidate.title
        if len(body) > 600:
            body = body[:600] + "…"
        source = candidate.source or _host(candidate.url) or "来源未标注"
        return [
            {"role": "system", "content": f"{_SYSTEM}\n\n{_RULES}"},
            {
                "role": "user",
                "content": f"来源：{source}\n信源分级：{candidate.tier}\n\n标题：{candidate.title}\n内容：{body}",
            },
        ]

    async def _once(self, messages: list[dict[str, str]]) -> tuple[int | None, Usage | None]:
        reply = await asyncio.wait_for(
            self.llm.complete(messages, max_tokens=self.config.score_max_tokens),
            self.config.score_timeout_seconds,
        )
        used = reply.usage() if (reply.prompt_tokens or reply.completion_tokens) else None
        return parse_score(reply.content), used

    async def score(self, candidate: Candidate) -> tuple[Score, list[Usage]]:
        """给一条候选打两次分。任何一次失败都记为未评分，**不**用另一次的结果顶替。"""
        messages = self.build_prompt(candidate)
        threshold = self.threshold_of(candidate.tier)
        result = Score(news_item_id=candidate.news_item_id, threshold=threshold, unstable_gap=self.unstable_gap)
        usages: list[Usage] = []
        values: list[int | None] = []
        for attempt in (1, 2):
            try:
                value, used = await self._once(messages)
            except Exception as exc:  # 单条打分失败不拖垮整批
                logger.warning(
                    "第 %d 次打分失败（条目 %d）: %s: %s", attempt, candidate.news_item_id, type(exc).__name__, exc
                )
                value, used = None, None
            values.append(value)
            if used is not None:
                usages.append(used)
        result.score1, result.score2 = values[0], values[1]
        result.reason = result.describe()
        if not result.scored:
            logger.warning("条目 %d 未取得两次可用评分，按未入选处理", candidate.news_item_id)
        return result, usages

    async def select(self, candidates: list[Candidate]) -> Selection:
        """逐条打分并给出精选结果。超过上限的候选直接丢弃并如实记录。

        顺序打分（不并发）是刻意的：本机网络对同一端点的并发调用实测不稳，
        而精选的耗时大头在推理 token 上，并发省下的时间抵不上失败重试。
        """
        if len(candidates) > self.config.max_candidates:
            logger.warning(
                "候选 %d 条超过精选上限 %d，只打分前 %d 条",
                len(candidates),
                self.config.max_candidates,
                self.config.max_candidates,
            )
            candidates = candidates[: self.config.max_candidates]
        selection = Selection()
        for candidate in candidates:
            score, usages = await self.score(candidate)
            selection.scores.append(score)
            selection.usage.extend(usages)
        logger.info(
            "精选完成：候选 %d 条，入选 %d 条，两次打分不一致 %d 条，未评分 %d 条",
            len(selection.scores),
            len(selection.selected),
            len(selection.unstable),
            len(selection.unscored),
        )
        return selection


async def save_scores(db: Database, selection: Selection) -> int:
    """把评分写入 `story_score`（同一篇文章只留最后一次）。返回写入条数。

    落库的目的是事后能解释"为什么它没进"：只把入选的记下来，读者问起落选原因时
    无据可查，也就无从改进标准。
    """
    if not selection.scores:
        return 0
    async with db.session() as session:
        for score in selection.scores:
            stored = await session.scalar(select(StoryScore).where(StoryScore.news_item_id == score.news_item_id))
            row = score.to_row()
            if stored is not None:
                stored.score1, stored.score2 = row.score1, row.score2
                stored.selected, stored.reason = row.selected, row.reason
                continue
            session.add(row)
        await session.commit()
    return len(selection.scores)


def usage_total(selection: Selection) -> dict[str, Any]:
    """汇总本次精选的 token 用量，便于接进成本统计。"""
    return {
        "calls": len(selection.usage),
        "prompt_tokens": sum(item.prompt_tokens for item in selection.usage),
        "completion_tokens": sum(item.completion_tokens for item in selection.usage),
    }
