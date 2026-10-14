"""
模块: result.board
职责: 把事件层的结果渲染成**聊天文本**——热度榜与日报可直接推送到微信 / QQ
依赖: core.db, core.log, result.digest, result.hot, core.models

为什么不复用网页的那套 JSON：聊天与网页的排版约束完全不同（聊天没有表格、
没有折行、消息还有平台长度上限，过长会被适配器切段，切在链接中间就废了）。
所以这里是**另一套呈现**，但口径必须与网页一致——参与者、热度、排序全部取自
`hot.ranking` 与 `digest.assemble`，不在这里重算任何指标。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem, ReportItem
from newsbot.result import digest as dg
from newsbot.result import hot

logger = get_logger(__name__)

# 聊天里最多列几条。热度榜在网页上是 20 条，聊天给 8 条就够——多出来的部分在
# 手机上是纯滚动负担；而日报本身就是精选后的少量条目，用 `digest.DEFAULT_LIMIT`
HOT_LIMIT = 8
# 展示时区的唯一出处是 `result.digest`（日报标题也用它）；这里只是给调用方一个入口，
# 免得为"传什么时区"再去 import 一次 digest
DEFAULT_TIMEZONE = dg.DEFAULT_TIMEZONE

# 标题截断长度。QQ / 微信一行放不下长英文标题，超长会让整条消息折行错乱；
# 截断处保留可辨识度（英文标题前 60 字符通常已含主体）
TITLE_CHARS = 60

# 趋势 → 中文标签。与网页共用同一套 grade 取值（hot.trend 的返回值）
TREND_LABEL = {"new": "新出现", "up": "升温", "down": "降温", "flat": "持平"}

# 订阅主题 → 任务类型。让用户能 `/订阅 热榜 21:00` 把事件层的榜单变成每日推送，
# 而不是又走一遍 Agent 现搜现写（那条链路正是事件层要取代的：查询时间不对、
# 来源重复、去重还会删掉正文引用过的编号）
BOARD_TOPICS: dict[str, str] = {
    "热榜": "hot",
    "热度": "hot",
    "热度榜": "hot",
    "热点": "hot",
    "日报": "digest",
    "精选": "digest",
    "日报精选": "digest",
}

BOARD_KINDS = frozenset({"hot", "digest"})


def _clip(text: str, limit: int = TITLE_CHARS) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _render_entry(index: int, entry: hot.HotEntry, url: str) -> str:
    """一条：序号 + 标题 + 指标 + 链接。

    指标只放"参与者家数"与"热度刻度"：聊天里没有表头，列多了读者不知道数字是什么。
    趋势只在非持平时写出来——每条都写"持平"等于没写。
    """
    line = f"{index}. {_clip(entry.title)}"
    marks = [f"{entry.participants} 家报道", f"热度 {entry.index}"]
    label = TREND_LABEL.get(entry.trend, entry.trend)
    if entry.trend != "flat" and label:
        # 必须用 `:.0%` 而不是 `:.0f}%`：`trend_pct` 是**比例**（0.159 = 15.9%），
        # 数值格式 `%` 不会自动乘 100——写成 `:.0f}%` 会让所有涨跌幅差 100 倍，
        # 而且因为四舍五入，±10% 以内的变化一律显示成 `0%`（真机实测出过
        # 「降温 -0%」：实际是 -15.9% 的纯时间衰减），读者完全看不出变化
        marks.append(f"{label} {entry.trend_pct:.0%}" if entry.trend_pct else label)
    line += f"\n　（{' · '.join(marks)}）"
    if url:
        line += f"\n　{url}"
    return line


async def hot_text(
    db: Database,
    *,
    limit: int = HOT_LIMIT,
    now: datetime | None = None,
    timezone: str = dg.DEFAULT_TIMEZONE,
) -> str:
    """热度榜的聊天文本。

    热度是**自己算的**，不是抓别家的榜单：参与者按独立主体去重（一家媒体发十篇只算
    一次），社区讨论算参与者但不计入独立来源数，进榜要求参与者 ≥2 且编辑类来源 ≥1。
    表头时刻按**展示时区**渲染（`dg._local`），否则 UTC+8 的用户会看到早 8 小时的
    时间，跨日时连日期都对不上。
    """
    moment = now or datetime.now(UTC)
    entries = await hot.ranking(db, moment, limit=limit)
    if not entries:
        return "热度榜为空。\n\n需要同一件事被至少两家独立来源报道才会进榜；先攒一些资讯再来看。"

    urls = await _latest_urls(db, [entry.story_id for entry in entries])
    lines = [
        f"热度榜 · {dg.local_time(moment, timezone):%m-%d %H:%M}",
        "48 小时窗口 · 24 小时半衰期 · 按独立来源计数（一家媒体发十篇只算一次）",
        "",
    ]
    lines.extend(_render_entry(index, entry, urls.get(entry.story_id, "")) for index, entry in enumerate(entries, 1))
    return "\n".join(lines)


async def new_event_text(
    db: Database,
    entries: list[hot.HotEntry],
    *,
    now: datetime | None = None,
    timezone: str = dg.DEFAULT_TIMEZONE,
) -> str:
    """新事件告警的聊天文本：一件事一条，与热度榜同一套指标与排版。

    为什么复用榜单的条目渲染而不是另写一套：告警里说的"热度 19.3 · 2 家报道 · 新出现"
    必须与用户随后打开的榜单**对得上**。两处各写一套，改了一处就会出现"推送里写 2 家、
    榜上写 3 家"这种没法解释的差异。
    """
    moment = now or datetime.now(UTC)
    urls = await _latest_urls(db, [entry.story_id for entry in entries])
    lines = [
        f"新事件 · {dg.local_time(moment, timezone):%m-%d %H:%M}",
        "（同一件事被至少两家独立来源报道才会进榜，以下按热度排序）",
        "",
    ]
    lines.extend(_render_entry(index, entry, urls.get(entry.story_id, "")) for index, entry in enumerate(entries, 1))
    return "\n".join(lines)


async def digest_text(
    db: Database,
    *,
    period: str = dg.DEFAULT_PERIOD,
    limit: int = dg.DEFAULT_LIMIT,
    now: datetime | None = None,
    timezone: str = dg.DEFAULT_TIMEZONE,
) -> str:
    """日报/周报/月报的聊天文本。

    直接复用 `digest.render()`——它本来就是纯文本、按行排版，没有网页专属结构。
    编排是**纯规则零模型调用**（可复算），只有事件综述那一步用了模型。
    """
    built = await dg.assemble(db, period=period, now=now, limit=limit, timezone=timezone)
    return built.render()


async def render(
    db: Database,
    kind: str,
    *,
    period: str = dg.DEFAULT_PERIOD,
    now: datetime | None = None,
    timezone: str = dg.DEFAULT_TIMEZONE,
) -> str:
    """按任务类型渲染。`kind` 不认识时抛 ValueError（调用方只应传 `BOARD_KINDS`）。"""
    if kind == "hot":
        return await hot_text(db, now=now, timezone=timezone)
    if kind == "digest":
        return await digest_text(db, period=period, now=now, timezone=timezone)
    raise ValueError(f"未知的事件层呈现类型: {kind!r}")


async def _latest_urls(db: Database, story_ids: list[int]) -> dict[int, str]:
    """每个事件取**最新那条报道**的网址，供聊天里点进去。

    取最新而不是最早：读者点进去想看的是当前进展，不是最初那条。事件页把两者都列出来，
    聊天里只给最新一条——消息变长比点错一条更糟。
    """
    async with db.session() as session:
        rows = (
            await session.execute(
                select(ReportItem.story_id, NewsItem.url, ReportItem.observed_at)
                .join(NewsItem, NewsItem.id == ReportItem.news_item_id)
                .where(ReportItem.story_id.in_(story_ids))
                .where(ReportItem.editorial.is_(True))
            )
        ).all()
    newest: dict[int, tuple[datetime, str]] = {}
    for story_id, url, observed_at in rows:
        if not url:
            continue
        moment = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
        current: Any = newest.get(story_id)
        if current is None or moment > current[0]:
            newest[story_id] = (moment, str(url))
    return {story_id: pair[1] for story_id, pair in newest.items()}
