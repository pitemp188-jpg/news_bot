"""
模块: dispatcher.collect
职责: 定时采集——按固定领域组拉订阅源的最新条目并入库，让热榜的进料不再依赖人问一句
依赖: core.db, core.log, dispatcher.ingest

为什么需要它：在它之前，资讯库只有两个入口——用户提问、以及定时订阅跑一次 Agent 检索。
两者都是**被动**的，于是库里一天只进两三次料（实测 10-14 当天 0 条：没人触发检索），
30 分钟一次的事件层刷新永远看到 `候选 0 条`。热榜本身是算出来的（聚簇→打分→热度），
但算的对象要先有人把料送进来，否则榜就是死的。

它刻意**不调用模型**：采集是"把这些源上最新的东西都取回来"，选什么、写什么不需要
判断力，交给模型只会让"漏了什么"不可观测，还平白多一笔开销与延迟。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.dispatcher.ingest import store_items

logger = get_logger(__name__)


class LatestFeed(Protocol):
    """能按时间窗取回订阅源最新条目的东西；由 `agent.tools.search.SearchTool` 实现。"""

    async def latest(self, *, days: int, limit: int, feed_timeout: float | None = None) -> list[dict[str, Any]]: ...


@dataclass
class CollectStats:
    """一轮采集的结果。数字要能回答"这轮到底有没有新东西"。"""

    fetched: int = 0
    written: int = 0
    failed: bool = False

    def line(self) -> str:
        if self.failed:
            return "采集失败（详见日志）"
        return f"采集：源上取回 {self.fetched} 条 → 新增入库 {self.written} 条"


async def collect(
    db: Database,
    tool: LatestFeed,
    *,
    days: int = 1,
    limit: int = 120,
    feed_timeout: float | None = None,
) -> CollectStats:
    """拉一轮订阅源并入库；任何异常都吞掉（按时跑的后台作业不该让服务不稳定）。

    入库去重交给 `ingest.store_items`（按网址）：采集每半小时跑一次，同一个源上
    没更新的条目会被反复取回，去重是常态而不是例外——所以 `written` 远小于 `fetched`
    是正常的，不是故障。
    """
    stats = CollectStats()
    try:
        items = await tool.latest(days=days, limit=limit, feed_timeout=feed_timeout)
    except Exception as exc:
        stats.failed = True
        logger.warning("订阅源采集失败: %s: %s", type(exc).__name__, exc)
        return stats

    stats.fetched = len(items)
    stats.written = await store_items(db, items, origin="采集")
    logger.info("定时采集完成：%s", stats.line())
    return stats
