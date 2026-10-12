"""
模块: result.hot
职责: 事件热度——48 小时窗口内按独立参与者计数、24 小时半衰期衰减，出榜单与逐小时快照
依赖: core.db, core.log, core.models, result.dedup
来源: 算法规格移植自 AIHOT（MIT，KKKKhazix/AIHOT）packages/backend/src/events/hot.ts 的
      heat-v1-48h-halflife24h，按本项目的数据模型与 SQLite 重写
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import HotSnapshot, ReportItem, Story

logger = get_logger(__name__)

# 规则版本：改了下面任何一个常量都要改它。榜单落库时带上版本，事后能解释"那天为什么
# 排成这样"——热度是相对量，换了口径的旧榜单和新榜单不可比。
RULE_VERSION = "heat-v1-48h-halflife24h"
WINDOW_HOURS = 48
HALF_LIFE_HOURS = 24
# 至少两个独立参与者、且至少一个编辑类来源才进榜：一条独家报道不是"热点"
MIN_PARTICIPANTS = 2
# 趋势对比的间隔：和 6 小时前比。涨得快的标出来，比看绝对值更有信息量
TREND_GAP_HOURS = 6
# 6 小时内新增参与者达到这个数、且占比过半 → surge
SURGE_MIN_NEW = 3
SURGE_RATIO = 0.5
# 涨幅超过这个比例 → rising
RISING_PCT = 0.15
# 首报在这个小时内 → 标"新"
NEW_HOURS = 6
# 榜单长度
RANKING_SIZE = 10


@dataclass(frozen=True)
class Signal:
    """某个独立参与者对某个事件的一次报道。

    `participant` 是**独立主体**而不是源：一家公司的多个渠道算一个、社区里每个作者各算
    一个。热度要回答的是"多少人在说这件事"，不是"抓到了几篇文章"。
    """

    participant: str
    observed_at: datetime
    editorial: bool = True


@dataclass(frozen=True)
class Heat:
    """某一时刻一个事件的热度。"""

    heat: float
    participants: int
    editorial_participants: int
    latest_at: datetime | None
    first_at: datetime | None
    # 6 小时内首次出现的参与者数，用于 surge 判定
    recent: int = 0

    @property
    def index(self) -> float:
        """给读者看的刻度：热度的 10 倍、一位小数。

        原始值上限很低（48 小时窗口、24 小时半衰期下最多约 2.0），直接展示会全是小数，
        读者看不出差别。
        """
        return round(self.heat * 100) / 10

    @property
    def qualifies(self) -> bool:
        """够不够进榜。"""
        return self.participants >= MIN_PARTICIPANTS and self.editorial_participants >= 1


def decay(hours: float) -> float:
    """按半衰期衰减：每过一个半衰期减半。"""
    return 0.5 ** (hours / HALF_LIFE_HOURS)


def heat_at(signals: Iterable[Signal], at: datetime) -> Heat:
    """算 `at` 时刻的热度。

    规则（照 AIHOT 的规格）：
    - 只看 `at` 之前 `WINDOW_HOURS` 小时内的证据；
    - 每个参与者只算一次，取窗口内**最新**的那次报道时间——重复抓取不叠加，一家媒体
      发十篇也只算一次；
    - 单次贡献按到 `at` 的时间差做半衰期衰减：越旧越小，但 48 小时窗口内不会归零。
    """
    since = at - timedelta(hours=WINDOW_HOURS)
    newest: dict[str, Signal] = {}
    for signal in signals:
        if signal.observed_at > at or signal.observed_at <= since:
            continue
        current = newest.get(signal.participant)
        if current is None or signal.observed_at > current.observed_at:
            newest[signal.participant] = signal
    if not newest:
        return Heat(heat=0.0, participants=0, editorial_participants=0, latest_at=None, first_at=None)

    heat = 0.0
    for signal in newest.values():
        hours = (at - signal.observed_at).total_seconds() / 3600
        heat += decay(hours)

    recent_cutoff = at - timedelta(hours=TREND_GAP_HOURS)
    all_times = [s.observed_at for s in signals if s.observed_at <= at]
    return Heat(
        heat=heat,
        participants=len(newest),
        editorial_participants=sum(1 for s in newest.values() if s.editorial),
        latest_at=max(s.observed_at for s in newest.values()),
        first_at=min(all_times) if all_times else None,
        # 「6 小时内新增」按参与者的**首次**出现时间算，而不是最后一篇的时间：
        # 一个参与者连续发三篇不算三个人新加入
        recent=sum(1 for p in newest if _first_seen(signals, p, at) > recent_cutoff),
    )


def _first_seen(signals: Iterable[Signal], participant: str, at: datetime) -> datetime:
    """某个参与者在 `at` 之前的最早一次报道时间。"""
    times = [s.observed_at for s in signals if s.participant == participant and s.observed_at <= at]
    return min(times) if times else at


def trend(now: Heat, before: Heat) -> tuple[str, float | None]:
    """与 TREND_GAP_HOURS 小时前比，返回（档位, 涨幅）。

    档位含义：`new` 之前没有热度（刚出现）、`up`/`down` 明显变化、`flat` 基本不变。
    阈值取 10%：低于它的涨跌在读者眼里没有意义，标出来只会让人误以为有事发生。
    """
    if before.heat <= 0:
        return "new", None
    pct = (now.heat - before.heat) / before.heat
    if pct > 0.1:
        return "up", pct
    if pct < -0.1:
        return "down", pct
    return "flat", pct


def badges(now: Heat, at: datetime, pct: float | None) -> list[str]:
    """给榜单条目打标记。

    - `surge`：6 小时内新增参与者达到阈值且占比过半——正在被大量讨论；
    - `new`：首报在 NEW_HOURS 小时内；
    - `rising`：涨幅超过 RISING_PCT（有 surge 时不再重复标 rising）。
    """
    marks: list[str] = []
    if now.recent >= SURGE_MIN_NEW and now.participants and now.recent / now.participants >= SURGE_RATIO:
        marks.append("surge")
    if now.first_at is not None and at - now.first_at < timedelta(hours=NEW_HOURS):
        marks.append("new")
    if "surge" not in marks and pct is not None and pct > RISING_PCT:
        marks.append("rising")
    return marks


@dataclass
class HotEntry:
    """榜单里的一条。"""

    story_id: int
    title: str
    heat: float
    index: float
    participants: int
    sources: int
    trend: str
    trend_pct: float | None
    badges: list[str]
    latest_at: datetime | None
    first_at: datetime | None
    participant_names: list[str]


async def signals_for(db: Database, story_ids: list[int] | None = None) -> dict[int, list[Signal]]:
    """读出事件的全部报道证据，按事件分组。

    只认**可公开的编辑类报道之外也算**：热度衡量讨论活跃度，社区讨论（editorial=False）
    同样算参与者，只是不进精选。数据都在 SQLite 里，一次查完比按事件逐个查快得多。
    """
    async with db.session() as session:
        statement = select(ReportItem.story_id, ReportItem.participant, ReportItem.observed_at, ReportItem.editorial)
        if story_ids:
            statement = statement.where(ReportItem.story_id.in_(story_ids))
        rows = (await session.execute(statement)).all()
    grouped: dict[int, list[Signal]] = {}
    for story_id, participant, observed_at, editorial in rows:
        grouped.setdefault(story_id, []).append(
            Signal(
                # 参与者为空时退回"未知"，否则所有空参与者的报道会被当成同一个人而合并
                participant=str(participant or f"unknown:{story_id}"),
                observed_at=observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC),
                editorial=bool(editorial),
            )
        )
    return grouped


async def ranking(db: Database, at: datetime | None = None, limit: int = RANKING_SIZE) -> list[HotEntry]:
    """算出 `at` 时刻的热点榜。

    排序只看热度，平局时更晚有新报道的在前——同一件事刚有进展却排在旧事件后面不合理。
    """
    moment = at or datetime.now(UTC)
    grouped = await signals_for(db)
    if not grouped:
        return []

    async with db.session() as session:
        titles = dict(
            (await session.execute(select(Story.id, Story.title))).all(),
        )

    scored: list[tuple[Heat, int, str, str, float | None, list[str]]] = []
    for story_id, signals in grouped.items():
        now = heat_at(signals, moment)
        if not now.qualifies:
            continue
        before = heat_at(signals, moment - timedelta(hours=TREND_GAP_HOURS))
        grade, pct = trend(now, before)
        scored.append((now, story_id, str(titles.get(story_id, "")), grade, pct, badges(now, moment, pct)))

    scored.sort(key=lambda row: (row[0].heat, row[0].latest_at or moment), reverse=True)
    return [
        HotEntry(
            story_id=story_id,
            title=title,
            heat=now.heat,
            index=now.index,
            participants=now.participants,
            sources=now.editorial_participants,
            trend=grade,
            trend_pct=pct,
            badges=marks,
            latest_at=now.latest_at,
            first_at=now.first_at,
            participant_names=[],
        )
        for now, story_id, title, grade, pct, marks in scored[:limit]
    ]


async def snapshot(db: Database, at: datetime | None = None) -> int:
    """把当前热度按小时落一份快照，返回写入条数。"""
    moment = at or datetime.now(UTC)
    hour = moment.replace(minute=0, second=0, microsecond=0)
    grouped = await signals_for(db)
    written = 0
    async with db.session() as session:
        for story_id, signals in grouped.items():
            now = heat_at(signals, moment)
            if not now.qualifies:
                continue
            existing = (
                await session.execute(
                    select(HotSnapshot).where(HotSnapshot.story_id == story_id, HotSnapshot.hour == hour)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(HotSnapshot(story_id=story_id, hour=hour, heat=now.heat, participants=now.participants))
            else:
                # 同一小时内重复算要覆盖：证据在变（又有新报道），旧值作废
                existing.heat = now.heat
                existing.participants = now.participants
            written += 1
        await session.commit()
    return written
