"""
模块: dispatcher.events
职责: 事件层刷新——聚簇未归组的资讯 → 两轮打分精选 → 落逐小时热度快照
依赖: core.config, core.db, core.llm, core.log, result.cluster, result.hot, result.selection
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.llm import LLM, Usage
from newsbot.core.log import get_logger
from newsbot.core.models import HotSnapshot, NewsItem, ReportItem, Story, StoryScore
from newsbot.result import cluster, hot, selection

logger = get_logger(__name__)


@dataclass
class RefreshStats:
    """一次刷新的结果，供日志与命令行展示。"""

    loaded: int = 0
    clustered: int = 0
    stories: int = 0
    assigned: int = 0
    scored: int = 0
    selected: int = 0
    unstable: int = 0
    snapshots: int = 0
    skipped_locked: int = 0
    usage: list[Usage] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"候选 {self.loaded} 条 → 归组 {self.clustered} 条 / 新建事件 {self.stories} 个"
            f"（写入归属 {self.assigned} 条）→ 打分 {self.scored} 条（入选 {self.selected}，"
            f"待复核 {self.unstable}）→ 热度快照 {self.snapshots} 条"
        )


async def reset(db: Database) -> dict[str, int]:
    """清空事件层（事件、归属、评分、热度快照），保留资讯库本身。

    为什么必须有这个能力：聚簇只会处理**尚未归组**的资讯，所以第一遍跑歪了以后,
    那些报道就永远停在错误的归属里——既不能重跑，也无法修正。算法调优、提示词改进
    之后都需要重来一遍，没有重建入口就只能手动删表。

    **只清事件层，不动 `news_item`**：原始资讯是采集成果，清掉就得重新抓一遍。
    人工锁定（`story.locked`）的归属同样会被清掉，所以这是一个明确的"重来"动作,
    不是日常操作（有 `--reset` 显式入口，不在自动刷新里）。
    """
    counts: dict[str, int] = {}
    async with db.session() as session:
        for model in (HotSnapshot, ReportItem, StoryScore, Story):
            # 先数再删：`session.execute(delete(...)).rowcount` 的类型标注在 SQLAlchemy 里
            # 是 CursorResult 上才有的属性，直接取会被 mypy 拦住；count 也更直观
            counts[model.__tablename__] = int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
            await session.execute(delete(model))
        await session.commit()
    logger.warning("事件层已重建：%s", counts)
    return counts


async def refresh(
    db: Database,
    llm: LLM,
    *,
    editorial: EditorialSection | None = None,
    days: int | None = None,
    now: datetime | None = None,
    do_cluster: bool = True,
    do_select: bool = True,
    do_snapshot: bool = True,
) -> RefreshStats:
    """把最近一段时间的资讯刷进事件层。

    三步的顺序是有依赖的，不能换、也不能省中间那步：
    1. **聚簇**：只有先归组，才有"同一件事被多少家独立来源报道过"这个量。
    2. **精选**：打分按 (报道, 门槛) 存进 `story_score`，日报靠它排序。
       它读的是**已归组的报道**，所以必须在聚簇之后。
    3. **热度快照**：读的是 `report_item` 的参与者，同样依赖第 1 步。它取的是
       **当下**的时刻而不是本次刷新的起始时刻，原因见下方注释。

    每一步失败都**不阻断后面**（各自 try 住）：聚簇失败时精选仍可对旧事件打分，
    快照失败也不影响已经落库的分组——一次刷新里任何一步都是"尽量多做一点"，
    而不是全有或全无。
    """
    config = editorial or EditorialSection()
    moment = now or datetime.now(UTC)
    window = days if days is not None else config.cluster_days
    stats = RefreshStats()

    if do_cluster:
        await _cluster(db, llm, config, window, moment, stats)
    if do_select:
        await _select(db, llm, config, moment, stats)
    if do_snapshot:
        # 快照的时间必须是**当下**，不能复用链路开头那个 `moment`：聚簇写入的
        # `report_item.observed_at` 是写入时刻，晚于开头几秒到几分钟，而 `heat_at`
        # 会跳过 `observed_at > at` 的证据（历史回溯时必须排除未来证据）。真实跑
        # 147 条时正因如此快照恒为 0 条——功能一直在静默空转，从不报错。
        try:
            stats.snapshots = await hot.snapshot(db, now or datetime.now(UTC))
        except Exception as exc:  # 快照失败不影响已落库的分组与评分
            logger.warning("热度快照失败: %s: %s", type(exc).__name__, exc)

    logger.info("事件层刷新完成：%s", stats.line())
    return stats


async def _cluster(
    db: Database,
    llm: LLM,
    config: EditorialSection,
    days: int,
    moment: datetime,
    stats: RefreshStats,
) -> None:
    """聚簇未归组的资讯。模型不可用时各自成事件（内容不丢，只是不做合并）。"""
    try:
        candidates = await cluster.load_candidates(db, days=days, now=moment, editorial=config)
    except Exception as exc:
        logger.warning("读取待聚簇资讯失败: %s: %s", type(exc).__name__, exc)
        return
    stats.loaded = len(candidates)
    if not candidates:
        logger.info("没有待归组的资讯（最近 %d 天内的都已归组）", days)
        return
    try:
        plan = await cluster.Clusterer(llm, config).assign(candidates)
        stats.usage.extend(plan.usage)
        result = await cluster.write_plan(db, plan, candidates)
    except Exception as exc:  # 聚簇失败不阻断精选与快照
        logger.warning("聚簇失败: %s: %s", type(exc).__name__, exc)
        return
    stats.clustered = sum(len(item.members) for item in plan.clusters)
    stats.stories = int(result.get("stories", 0))
    stats.assigned = int(result.get("items", 0))
    stats.skipped_locked = int(result.get("skipped_locked", 0))


async def _select(
    db: Database,
    llm: LLM,
    config: EditorialSection,
    moment: datetime,
    stats: RefreshStats,
) -> None:
    """给尚未打分的报道打两轮分。

    只打**没有评分记录**的报道（`StoryScore` 不存在）：打分是两次模型调用，
    每次刷新都重打一遍全部报道既贵又没意义——内容没变，分数也不会变。
    优先打最近的：上限（`refresh_max_scores`）兜住成本，超出的等下轮。
    """
    try:
        candidates = await _pending_scores(db, config, moment, limit=config.refresh_max_scores)
    except Exception as exc:
        logger.warning("读取待打分报道失败: %s: %s", type(exc).__name__, exc)
        return
    if not candidates:
        logger.info("没有待打分的报道")
        return
    try:
        result = await selection.Selector.from_config(llm, config).select(candidates)
        await selection.save_scores(db, result)
    except Exception as exc:
        logger.warning("精选打分失败: %s: %s", type(exc).__name__, exc)
        return
    stats.usage.extend(result.usage)
    stats.scored = len(result.scores)
    stats.selected = len(result.selected)
    stats.unstable = len(result.unstable)


async def _pending_scores(
    db: Database,
    config: EditorialSection,
    moment: datetime,
    *,
    limit: int,
) -> list[selection.Candidate]:
    """待打分的报道：已归入事件、未打过分、最近 `cluster_days` 天内。

    限定"已归入事件"是关键：精选的结论要挂到事件上才会被日报用到，给一条还没归组的
    报道打分，它既不会进日报也无法解释——那两次模型调用就白花了。
    """
    since = moment - timedelta(days=config.cluster_days)
    async with db.session() as session:
        scored = select(StoryScore.news_item_id)
        rows = (
            await session.execute(
                select(NewsItem, ReportItem)
                .join(ReportItem, ReportItem.news_item_id == NewsItem.id)
                .join(Story, Story.id == ReportItem.story_id)
                .where(Story.locked.is_(False))
                .where(NewsItem.id.not_in(scored))
                .where(NewsItem.fetched_at >= since)
                .order_by(NewsItem.fetched_at.desc())
                .limit(limit)
            )
        ).all()
    return [
        selection.Candidate(
            news_item_id=item.id,
            title=item.title or "",
            text=item.summary or "",
            source=item.source or "",
            url=item.url or "",
            tier=report.tier or config.tier_of(item.source or "", item.url or ""),
        )
        for item, report in rows
        if report.editorial
    ]
