"""
模块: tests.unit.dispatcher.test_collect
职责: 校验定时采集——拉源、入库、失败不影响服务
依赖: dispatcher.collect
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from newsbot.core.db import Database
from newsbot.core.models import NewsItem
from newsbot.dispatcher.collect import collect

ITEM = {
    "title": "某公司发布新一代推理芯片",
    "url": "https://example.com/a",
    "snippet": "官方称性能提升三成",
    "source": "a.com",
    "published": "2026-10-13T08:30:00Z",
    "weight": 1.0,
}


class FakeFeed:
    """替身：记录被要求的窗口，返回预设条目。"""

    def __init__(self, items: list[dict[str, Any]] | None = None, *, boom: bool = False) -> None:
        self.items = items if items is not None else [dict(ITEM)]
        self.boom = boom
        # 记下被要求的时间窗、上限与单源超时：三者都要真的传到取数口，
        # 否则「采集用检索的 8s 超时」这类问题会被静默吞掉（实测漏掉 1/3 的源）
        self.calls: list[tuple[int, int, float | None]] = []

    async def latest(
        self, *, days: int = 1, limit: int = 120, feed_timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((days, limit, feed_timeout))
        if self.boom:
            raise RuntimeError("源全挂了")
        return list(self.items)


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'collect.db'}")
    await db.prepare()
    return db


async def _count(db: Database) -> int:
    async with db.session() as session:
        return len(list((await session.execute(select(NewsItem))).scalars()))


async def test_collect_stores_fetched_items(tmp_path: Path) -> None:
    """采集到的条目要真的落进资讯库，否则事件层刷新永远是「候选 0 条」。"""
    db = await _make_db(tmp_path)
    try:
        tool = FakeFeed()
        stats = await collect(db, tool, days=1, limit=50)
        assert stats.fetched == 1
        assert stats.written == 1
        assert await _count(db) == 1
        assert tool.calls == [(1, 50, None)], "时间窗与上限要按调用方给的传下去"
    finally:
        await db.dispose()


async def test_collect_is_idempotent(tmp_path: Path) -> None:
    """同一批条目再采一次不重复入库——采集每半小时跑一次，重复是常态。"""
    db = await _make_db(tmp_path)
    try:
        tool = FakeFeed()
        assert (await collect(db, tool)).written == 1
        second = await collect(db, tool)
        assert second.fetched == 1, "取回条数照实记"
        assert second.written == 0, "已入库的不该再写"
        assert await _count(db) == 1
    finally:
        await db.dispose()


async def test_collect_swallows_source_failure(tmp_path: Path) -> None:
    """源全挂时只记失败，不向上抛——它是按时跑的后台作业，不能拖垮服务。"""
    db = await _make_db(tmp_path)
    try:
        stats = await collect(db, FakeFeed(boom=True))
        assert stats.failed is True
        assert stats.fetched == 0 and stats.written == 0
        assert "失败" in stats.line()
        assert await _count(db) == 0
    finally:
        await db.dispose()


async def test_collect_passes_the_feed_timeout(tmp_path: Path) -> None:
    """单源超时必须能透传。

    回归用的真实发现：采集一度沿用检索的 8s 单源上限，实测 15 个源里有 5 个
    （venturebeat / theverge / tomshardware / lobste.rs / hnrss）返回不了，
    等于稳定漏掉三分之一的内容。检索要抢时间，采集是后台作业，两者必须分开。
    """
    db = await _make_db(tmp_path)
    try:
        tool = FakeFeed()
        await collect(db, tool, feed_timeout=25.0)
        assert tool.calls[0][2] == 25.0
    finally:
        await db.dispose()


async def test_collect_drops_listing_pages(tmp_path: Path) -> None:
    """采集路径与检索路径共用同一套质量判据：栏目页不许进来。"""
    db = await _make_db(tmp_path)
    try:
        tool = FakeFeed(
            [
                dict(ITEM),
                {"title": "股票频道 - 东方财富网", "url": "https://stock.eastmoney.com/", "snippet": ""},
            ]
        )
        stats = await collect(db, tool)
        assert stats.fetched == 2
        assert stats.written == 1, "栏目页不该入库"
        assert await _count(db) == 1
    finally:
        await db.dispose()


async def test_collect_on_empty_result(tmp_path: Path) -> None:
    """源里没有新条目时是一句平静的「取回 0 条」，不是错误。"""
    db = await _make_db(tmp_path)
    try:
        stats = await collect(db, FakeFeed([]))
        assert stats.failed is False
        assert "取回 0 条" in stats.line()
    finally:
        await db.dispose()


def test_collect_line_is_self_describing_without_a_label() -> None:
    """统计行只说做了什么，场景由调用方补。

    回归用例（真机日志抓到）：`line()` 一度自带「采集：」前缀，而调用方又写成
    `"定时采集：%s"`，日志读成「定时采集：采集：源上取回 146 条」——同一个词连着两次。
    约定与 `RefreshStats.line()` 一致：统计行不带场景前缀。
    """
    from newsbot.dispatcher.collect import CollectStats

    line = CollectStats(fetched=146, written=36).line()
    assert line == "源上取回 146 条 → 新增入库 36 条"
    assert "采集" not in line, "场景由调用方补，统计行里不该再出现"

    failed = CollectStats(failed=True).line()
    assert failed == "失败（详见日志）", "失败也要能拼进「定时采集：…」里读通"
