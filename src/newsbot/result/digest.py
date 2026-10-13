"""
模块: result.digest
职责: 日报/周报/月报编排——一个事件一条，按重要度排序，纯规则组装不调用模型
依赖: core.config, core.db, core.log, core.models, result.hot
来源: 编排规则（一个事件一条 / 有新增才出后续 / 重要度公式）移植自 AIHOT（MIT，KKKKhazix/AIHOT）
      packages/backend/src/industry/digest.ts，按本项目的数据模型重写
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem, ReportItem, Story, StoryScore
from newsbot.result import hot

logger = get_logger(__name__)

# 周期 → 回看天数
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
DEFAULT_PERIOD = "daily"
# 日报最多列这么多条事件。条数不是靠"模型觉得够了"定的，而是版面决定的
DEFAULT_LIMIT = 12
# 取热度榜时放宽到这么多条：日报要按热度修正重要度，只取前 12 条会让第 13 名之后
# 的事件拿不到热度口径的参与者数，排序因此与热度榜不一致
HEAT_LOOKUP = 200

# 重要度的两个加权项。为什么是这两个、以及为什么用 log：
# 参与者数量对"这件事有多重要"的贡献是**边际递减**的——2 家报到比 1 家重要得多，
# 20 家比 10 家只重要一点。线性加会让一个被全网点名的发布会永远压住一切，所以取
# log2(1+n)。官方一手来源（T1）单独加分：它是事件的**锚**，读者要从它开始看。
PARTICIPANT_WEIGHT = 5.0
OFFICIAL_BONUS = 5.0
# 后续进展排在前面是错的（读者先要知道发生了什么），所以"本期只有后续"的事件扣分
FOLLOW_UP_PENALTY = 6.0


def importance(score: int, participants: int, *, official: bool = False, follow_up_only: bool = False) -> float:
    """事件在日报里的重要度。

    公式：`score + 5*log2(1+participants) + (official ? 5 : 0) - (onlyFollowUp ? 6 : 0)`。

    `score` 是精选分（0～10），参与者数量是**边际递减**的加分项，官方来源是锚点加分，
    "本期只有后续进展"扣分——读者需要先知道发生了什么，把后续放在前面是倒置。
    组装本身不调用模型：重要度必须是**可复算**的，否则同一批数据两次跑出两个排序，
    出了问题无从解释。
    """
    value = float(score) + PARTICIPANT_WEIGHT * math.log2(1 + max(participants, 0))
    if official:
        value += OFFICIAL_BONUS
    if follow_up_only:
        value -= FOLLOW_UP_PENALTY
    return round(value, 3)


@dataclass
class Entry:
    """日报里的一条：一个事件。"""

    story_id: int
    title: str
    digest: str = ""
    url: str = ""
    score: int = 0
    participants: int = 0
    official: bool = False
    follow_up_only: bool = False
    heat: float = 0.0
    importance: float = 0.0
    latest_at: datetime | None = None
    titles: list[str] = field(default_factory=list)

    def line(self) -> str:
        """一条事件的纯文本呈现。不调用模型，也不需要模型改写措辞。"""
        head = f"{self.title}"
        tail = f"（{self.participants} 家报道"
        if self.official:
            tail += " · 官方一手"
        if self.follow_up_only:
            tail += " · 后续进展"
        tail += "）"
        body = self.digest.strip() or "；".join(self.titles[:3])
        text = f"- {head}{tail}"
        if body:
            text += f"\n  {body}"
        if self.url:
            text += f"\n  {self.url}"
        return text


@dataclass
class Digest:
    """一份编排好的日报/周报/月报。"""

    period: str
    since: datetime
    until: datetime
    entries: list[Entry] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def title(self) -> str:
        label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(self.period, "日报")
        return f"{self.until:%Y-%m-%d} {label}"

    def render(self) -> str:
        if not self.entries:
            return f"{self.title}\n\n本期没有达到入选标准的内容。"
        lines = [self.title, ""]
        lines.extend(entry.line() for entry in self.entries)
        return "\n".join(lines)


def within_period(story: Story, entry_at: datetime, since: datetime, until: datetime) -> bool:
    """事件是否属于本期：本期内有新报道（`last_seen_at` 落在区间内）就算。

    用 `last_seen_at` 而不是 `first_seen_at`：一个上周开始、今天有进展的事件属于今天，
    按首次出现筛会让它永远不再出现在日报里。
    """
    stamp = _as_utc(story.last_seen_at)
    return since <= stamp <= until


async def assemble(
    db: Database,
    *,
    period: str = DEFAULT_PERIOD,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Digest:
    """组装一份日报/周报/月报。

    **全部用规则**：哪些事件入选、怎么排序、每条写什么，都由可复算的规则决定。
    组装阶段调模型会让"为什么今天这条排第一"变成不可解释的，也让每次重跑结果不同。
    """
    moment = now or datetime.now(UTC)
    days = PERIOD_DAYS.get(period, PERIOD_DAYS[DEFAULT_PERIOD])
    since = moment - timedelta(days=days)
    digest = Digest(period=period if period in PERIOD_DAYS else DEFAULT_PERIOD, since=since, until=moment)
    async with db.session() as session:
        stories = (await session.scalars(select(Story).where(Story.last_seen_at >= since))).all()
        if not stories:
            return digest
        ids = [story.id for story in stories]
        rows = (
            await session.execute(
                select(ReportItem, NewsItem, StoryScore)
                .join(NewsItem, NewsItem.id == ReportItem.news_item_id)
                .outerjoin(StoryScore, StoryScore.news_item_id == ReportItem.news_item_id)
                .where(ReportItem.story_id.in_(ids))
            )
        ).all()

    grouped: dict[int, list[Any]] = {}
    for report, item, score in rows:
        grouped.setdefault(report.story_id, []).append((report, item, score))

    dropped = {"period": 0, "duplicate_follow_up": 0, "not_selected": 0}
    entries: list[Entry] = []
    for story in stories:
        records = grouped.get(story.id, [])
        if not records or not within_period(story, moment, since, moment):
            dropped["period"] += 1
            continue
        fresh = [
            (report, item, score) for report, item, score in records if since <= _as_utc(report.observed_at) <= moment
        ]
        # 本期没有任何新报道 = 旧闻，不进本期
        if not fresh:
            dropped["period"] += 1
            continue
        # **精选就是入选判定**：本期没有任何一条报道过入选门槛的事件不进日报。
        # 不读 `selected` 会让精选对日报毫无影响——实测跑完真实数据后，精选入选 6 条
        # 而日报照旧列了 12 条，整条链路等于白接。
        # 未评分的条目按未入选处理（`Score.selected` 的既有语义），所以打分没跑到的事件
        # 不会混进来；这是"宁可少列"的方向，与精选的失败方向一致。
        if not _selected(fresh):
            dropped["not_selected"] += 1
            continue
        entries.append(_entry(story, records, fresh))

    # 先并进全网热度（热度与所选周期无关），再按重要度排序——重要度里含参与者数量，
    # 不先补上热度口径的参与者数就会按残缺的数据排序
    decorate(entries, await hot.ranking(db, moment, limit=HEAT_LOOKUP))
    entries.sort(key=lambda entry: (entry.importance, entry.participants, entry.heat), reverse=True)
    digest.skipped = dropped
    digest.entries = entries[:limit]
    logger.info(
        "编排 %s：%d 个事件在期内，输出 %d 条（旧闻跳过 %d，超出上限 %d）",
        digest.period,
        len(entries),
        len(digest.entries),
        dropped["period"],
        max(0, len(entries) - limit),
    )
    return digest


def _selected(fresh: list[Any]) -> bool:
    """本期是否有报道通过了精选。

    判据是**本期**的报道：上周入选、本周只是被重新抓取的事件不该再进今天的日报。
    `score` 为 None（还没打分）按未入选处理——与 `Score.selected` 对未评分的语义一致，
    失败方向同样选"少列一条"而不是"全塞进去"。
    """
    return any(score is not None and bool(score.selected) for _, _, score in fresh)


def _entry(story: Story, records: list[Any], fresh: list[Any]) -> Entry:
    """把一个事件压成一条。

    **一个事件一条**：组内多家报道合并成一条，编号不并列——读者要的是进展，
    不是同一消息的多份副本。`follow_up_only` 表示本期**只有后续进展**（没有一手报道
    在本期出现），这种事件要排在后面。标题用**事件的标题**（归组时定的）,
    不是某一篇报道的标题——后者会把"谁先报的"当成"这是什么事"。
    """
    participants = {str(report.participant or "") for report, _, _ in records}
    participants.discard("")
    scores = [int(score.score1 + score.score2) // 2 for _, _, score in records if score is not None]
    official = any(report.tier == "T1" for report, _, _ in records)
    follow_up_only = all(report.role != "primary" for report, _, _ in fresh)
    newest = max(fresh, key=lambda row: _as_utc(row[0].observed_at))
    titles = [item.title for _, item, _ in records if item.title]
    score = max(scores) if scores else 0
    return Entry(
        story_id=story.id,
        title=story.title or (titles[0] if titles else f"事件 {story.id}"),
        digest=story.digest or "",
        url=newest[1].url or "",
        score=score,
        participants=len(participants),
        official=official,
        follow_up_only=follow_up_only,
        importance=importance(score, len(participants), official=official, follow_up_only=follow_up_only),
        latest_at=_as_utc(newest[0].observed_at),
        titles=titles,
    )


def decorate(entries: list[Entry], ranking: list[hot.HotEntry]) -> None:
    """把热度榜的结果并进条目，并用热度口径的参与者数**重算**重要度。

    热度是全网 48 小时窗口的口径，与所选周期无关——正是"这件事现在有多少人在说"
    这一维度，日报的排序需要它。
    """
    heat_of = {row.story_id: row for row in ranking}
    for entry in entries:
        row = heat_of.get(entry.story_id)
        if row is not None:
            entry.heat = row.heat
            entry.participants = max(entry.participants, row.participants)
        entry.importance = importance(
            entry.score,
            entry.participants,
            official=entry.official,
            follow_up_only=entry.follow_up_only,
        )


def _as_utc(value: datetime) -> datetime:
    """SQLite 不保留时区，读回来是 naive；约定按 UTC 解释。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
