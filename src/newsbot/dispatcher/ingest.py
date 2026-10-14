"""
模块: dispatcher.ingest
职责: 来源条目入库——挡掉列表页/搜索页、按网址去重、补齐摘要与内容指纹
依赖: core.db, core.log, core.models, core.quality, result.dedup

为什么抽成独立模块：入库曾只有一条路径（Agent 检索之后），所以它长在 `pipeline` 里。
定时采集也要入库，而两条路径必须**用同一套口径**——列表页判据、去重键、指纹算法
只要分叉，库里就会出现"检索进来的被挡掉、采集进来的混进去"这种无从排查的差异。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import NewsItem
from newsbot.core.quality import is_listing
from newsbot.result.dedup import content_digest, simhash64, simhash_hex, url_digest

logger = get_logger(__name__)


def published_at(raw: Any) -> datetime | None:
    """把来源里的 ISO 发布时间解析成 datetime；解析不了就返回 None。

    容错而非报错：有的源不给日期、有的格式不规范，这些都不该让入库失败——只是
    这一条的"新鲜度未知"，模型会看到「日期未知」。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("无法解析的发布时间: %r", text[:40])
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


async def store_items(db: Database, items: list[dict[str, str]], *, origin: str = "检索") -> int:
    """把新来源写入资讯库，供后续任务复用、聚簇与去重；返回实际写入条数。

    **列表页 / 搜索页不入库**：Agent 为了找线索会去抓 GitHub Trending、财经频道、
    搜索结果页，那些页面一旦入库就会变成"报道"，还得跟真文章一起去聚簇。
    实测 158 条里有 11 条是这类页面（7%），其中两条互相聚成了一个"事件"并占了
    热度榜首位。判据与实测依据见 `core.quality`。

    `origin` 只进日志，用来区分这批是检索来的还是定时采集来的——排查"这条脏数据
    是谁写进来的"时，有它就能直接定位到路径。
    """
    if not items:
        return 0
    kept = [item for item in items if not is_listing(str(item.get("title", "")), str(item.get("url", "")))]
    if len(kept) != len(items):
        logger.info("%s：跳过 %d 条列表页 / 搜索页来源，不入资讯库", origin, len(items) - len(kept))
    if not kept:
        return 0

    url_hashes = [url_digest(str(item.get("url", ""))) for item in kept]
    written = 0
    try:
        async with db.session() as session:
            existing = set(
                (await session.execute(select(NewsItem.url_hash).where(NewsItem.url_hash.in_(url_hashes)))).scalars()
            )
            for item, digest in zip(kept, url_hashes, strict=True):
                if not digest or digest in existing:
                    continue
                # 入库用的内容要与去重时的判据一致（标题 + 摘要），否则历史指纹永远命中不了
                body = f"{item.get('title', '')} {item.get('snippet', '')}".strip() or str(item.get("url", ""))
                session.add(
                    NewsItem(
                        url=str(item.get("url", "")),
                        url_hash=digest,
                        title=str(item.get("title", "")),
                        source=str(item.get("source", "")),
                        # 摘要存下来是给聚簇用的（只凭标题判"是不是同一件事"会漏掉
                        # 中英文互译的同一件事）。它与去重判据同源，改动要一起改
                        summary=str(item.get("snippet", "") or ""),
                        # 发布时间必须存：它是判断"这条新不新"的依据。此前一直留空，
                        # 于是模型看不到任何日期，把几天前的旧稿当成"最新进展"；
                        # news_db 也只能拿抓取时间当新鲜度，旧闻重抓就被算成新的。
                        published_at=published_at(item.get("published")),
                        content_hash=content_digest(body),
                        simhash=simhash_hex(simhash64(body)),
                        fetched_at=datetime.now(UTC),
                    )
                )
                existing.add(digest)
                written += 1
            await session.commit()
    except Exception as exc:  # 入库失败不应影响调用方（投递或本轮采集）
        logger.warning("%s：资讯入库失败: %s: %s", origin, type(exc).__name__, exc)
        return 0
    return written
