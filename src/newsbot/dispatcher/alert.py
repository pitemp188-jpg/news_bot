"""
模块: dispatcher.alert
职责: 新事件告警——把"刚出现且够格"的事件主动推给用户，而不是等他来问
依赖: core.db, core.log, core.models, gateway 的投递接口（由 app 注入）, result.board, result.hot

判定为什么是「刚出现 + 通过入选门槛」两条：
- **刚出现**（`trend == "new"`，6 小时前热度为 0）——已经推过的事件不再打扰；
  这条与榜单上「新出现」的标签同一个口径，用户在推送里看到的词与榜上对得上。
- **通过入选门槛**——光"有多家在说"不等于"值得看"。门槛就是日报用的那套打分
  （按信源分级），所以告警推的是"日报里也会收的东西"，而不是"被转得最多的东西"。

节流与首轮静默都是**护栏**，不是偏好：没有它们，上线第一次刷新会把库里积压的
几十个历史事件一次推光，之后就永远不敢开这个开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Delivery, ReportItem, Story, StoryScore
from newsbot.result import board, hot

logger = get_logger(__name__)

# 扫榜深度。新事件按热度排可能不在最前面，只取榜面前几条会漏；100 名之外的
# "新事件"本来也不值得单独推送（那是长尾噪音，不是热点）
SCAN_LIMIT = 100


class Sender(Protocol):
    """投递接口，与 `dispatcher.pipeline.Sender` 形状一致（同一个网关实现）。"""

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool: ...


@dataclass
class AlertStats:
    """一轮告警的结果；每个字段都对应一个"为什么没推"。"""

    candidates: int = 0
    sent: int = 0
    skipped_unselected: int = 0
    bootstrapped: int = 0
    failed: int = 0
    note: str = ""

    def line(self) -> str:
        if not self.candidates:
            return self.note or "没有新事件"
        base = (
            f"新事件 {self.candidates} 个 → 推送 {self.sent} 条"
            f"（未过入选门槛 {self.skipped_unselected}，首轮静默 {self.bootstrapped}，失败 {self.failed}）"
        )
        # 后面的 `note` 不能丢：它写着"为什么一条都没发"（没有目标 / 通道未就绪）。
        # 把原因吞掉，日志里就只剩"推送 0 条"，与"确实没有新事件"分不出来
        return f"{base}：{self.note}" if self.note else base


async def resolve_target(db: Database, platform: str, chat_id: str) -> tuple[str, str] | None:
    """决定推给谁：配置里写死了就用它，没写就回退到最近一次成功投递过的会话。

    回退的依据是 `delivery` 表里状态为 `sent` 的最近一行——那正是"用户实际在用的
    那个会话"。平台有主动推送限制，只有用户先给机器人发过消息才有可用目标，
    所以"最近成功投递过"这个条件恰好保证了目标可用。
    """
    if chat_id:
        return platform, chat_id
    async with db.session() as session:
        row = (
            await session.execute(
                select(Delivery.platform, Delivery.chat_id)
                .where(Delivery.status == "sent")
                .order_by(Delivery.id.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        logger.warning("没有可用的告警目标：未配置 alert_chat_id，且库里没有任何成功投递记录")
        return None
    return str(row[0]), str(row[1])


async def _selected(db: Database, story_ids: list[int]) -> set[int]:
    """这些事件里，哪些事件的报道**通过了入选门槛**（与日报同一套判据）。"""
    if not story_ids:
        return set()
    async with db.session() as session:
        rows = await session.execute(
            select(ReportItem.story_id)
            .join(StoryScore, StoryScore.news_item_id == ReportItem.news_item_id)
            .where(ReportItem.story_id.in_(story_ids), StoryScore.selected.is_(True))
            .distinct()
        )
    return {int(row[0]) for row in rows}


async def _never_alerted(db: Database) -> bool:
    """库里从没推过任何告警——用来识别"上线首轮"。"""
    async with db.session() as session:
        count = (
            await session.execute(select(func.count()).select_from(Story).where(Story.alerted_at.is_not(None)))
        ).scalar_one()
    return int(count) == 0


async def _mark(db: Database, story_ids: list[int], moment: datetime) -> None:
    """把这些事件记为已告警；必须在**推送成功之后**调用，否则失败的推送不会再重试。"""
    if not story_ids:
        return
    async with db.session() as session:
        rows = (await session.execute(select(Story).where(Story.id.in_(story_ids)))).scalars().all()
        for row in rows:
            row.alerted_at = moment
        await session.commit()


async def notify(
    db: Database,
    sender: Sender | None,
    *,
    platform: str = "qqbot",
    chat_id: str = "",
    max_per_run: int = 3,
    bootstrap: bool = True,
    now: datetime | None = None,
    timezone: str = board.DEFAULT_TIMEZONE,
) -> AlertStats:
    """把刚出现且够格的事件推给用户；返回本轮统计。

    三轮筛选的顺序是有意的：先按"没推过"去重（最便宜，纯查库），再看入选门槛
    （要连表），最后才渲染与投递（要发网络请求）。顺序反了会在不推的日子也白做重活。
    """
    stats = AlertStats()
    moment = now or datetime.now(UTC)
    entries = await hot.ranking(db, moment, limit=SCAN_LIMIT)
    fresh = [entry for entry in entries if entry.trend == "new"]
    if not fresh:
        stats.note = "没有新出现的事件"
        return stats
    if sender is None:
        stats.note = "投递通道未就绪，跳过告警"
        return stats

    async with db.session() as session:
        alerted = dict(
            (
                await session.execute(
                    select(Story.id, Story.alerted_at).where(Story.id.in_([e.story_id for e in fresh]))
                )
            ).all()
        )
    unseen = [entry for entry in fresh if alerted.get(entry.story_id) is None]
    stats.candidates = len(unseen)
    if not unseen:
        stats.note = "新出现的事件都已经推送过"
        return stats

    picked = await _selected(db, [entry.story_id for entry in unseen])
    ready = [entry for entry in unseen if entry.story_id in picked]
    stats.skipped_unselected = len(unseen) - len(ready)
    if not ready:
        stats.note = "新出现的事件都没过入选门槛"
        return stats

    if bootstrap and await _never_alerted(db):
        # 上线首轮：库里积压的历史事件一次推不完，也不该推——只做标记，从下一轮开始
        # 才推"真的刚出现"的。没有这道护栏，这个开关第一次打开就是一次轰炸
        await _mark(db, [entry.story_id for entry in ready], moment)
        stats.bootstrapped = len(ready)
        logger.info("首次运行：跳过 %d 个历史事件的告警，只标记不推送", len(ready))
        return stats

    target = await resolve_target(db, platform, chat_id)
    if target is None:
        stats.note = "没有可用的告警目标"
        return stats
    target_platform, target_chat = target
    batch = ready[:max_per_run]
    text = await board.new_event_text(db, batch, now=moment, timezone=timezone)
    if await sender.send(platform=target_platform, chat_id=target_chat, text=text):
        await _mark(db, [entry.story_id for entry in batch], moment)
        stats.sent = len(batch)
    else:
        stats.failed = len(batch)
    logger.info("新事件告警：%s", stats.line())
    return stats
