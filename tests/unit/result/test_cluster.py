"""
模块: tests.unit.result.test_cluster
职责: 校验事件聚簇——候选预筛、关系解析、不确定合并、复核拆组、角色判定与落库
依赖: result.cluster
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from tests.fakes.llm import FakeLLM, reply

from newsbot.core.config import EditorialSection
from newsbot.core.db import Database
from newsbot.core.models import NewsItem, ReportItem, Story
from newsbot.result import cluster as cl

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _candidate(item_id: int, title: str, *, text: str = "", source: str = "量子位", url: str = "") -> cl.Candidate:
    return cl.Candidate(
        news_item_id=item_id,
        title=title,
        text=text,
        source=source,
        url=url or f"https://qbitai.com/{item_id}",
        published_at=NOW - timedelta(hours=item_id),
    )


# ── 关系与复核解析 ──


def test_parse_relations_reads_well_formed_lines() -> None:
    found = cl.parse_relations("1,3,same\n2,4,follow_up", {1, 2, 3, 4})
    assert found[(1, 3)] == cl.REL_SAME
    assert found[(2, 4)] == cl.REL_FOLLOW_UP


def test_parse_relations_normalises_pair_order() -> None:
    assert cl.parse_relations("4,2,same", {2, 4}) == {(2, 4): cl.REL_SAME}


def test_parse_relations_ignores_unknown_relation_and_unknown_ids() -> None:
    """越界编号与不认识的词都要丢掉：把 `maybe` 当成结论就是替模型猜。"""
    assert cl.parse_relations("1,2,maybe", {1, 2}) == {}
    assert cl.parse_relations("1,99,same", {1, 2}) == {}


def test_parse_relations_ignores_prose() -> None:
    text = "我的判断如下：\n1,2,same\n以上。"
    assert cl.parse_relations(text, {1, 2}) == {(1, 2): cl.REL_SAME}


def test_parse_relations_returns_empty_for_empty_text() -> None:
    assert cl.parse_relations("", {1, 2}) == {}


def test_parse_vetoes_reads_group_numbers() -> None:
    assert cl.parse_vetoes("2,no", 3) == {1}
    assert cl.parse_vetoes("1,no\n3,不同", 3) == {0, 2}


def test_parse_vetoes_accepts_bare_no_for_a_single_group() -> None:
    assert cl.parse_vetoes("no", 1) == {0}


def test_parse_vetoes_ignores_confirmations() -> None:
    """复核只需要说出**不成立**的组，确认的组不用输出，也不该被当成否决。"""
    assert cl.parse_vetoes("1,same", 3) == set()


def test_parse_vetoes_ignores_out_of_range() -> None:
    assert cl.parse_vetoes("9,no", 3) == set()


# ── 候选预筛 ──


def test_candidate_pairs_keeps_shared_identifier() -> None:
    """共带数字的标识符是唯一能跨中英文对齐同一件事的词法证据，必须直接入选。"""
    left = _candidate(1, "Qwen-Image 2.1 Turbo 发布", source="qbitai")
    right = _candidate(2, "阿里发布 qwen-image-2.1-turbo 模型", source="venturebeat")
    assert cl.candidate_pairs([left, right], floor=0.9) == [(1, 2)]


def test_candidate_pairs_drops_unrelated_titles() -> None:
    """预筛阈值虽低，但毫无交集的标题仍要被挡掉——否则模型要看的配对会爆炸。"""
    left = _candidate(1, "苹果定档秋季发布会", source="ifanr")
    right = _candidate(2, "三星减产存储芯片", source="solidot")
    assert cl.candidate_pairs([left, right], floor=0.08) == []


def test_candidate_pairs_matches_on_summary_text() -> None:
    """正文片段也算：很多条目的标题措辞完全不同，只有摘要有共同词。"""
    left = _candidate(1, "某公司发布新一代芯片", text="性能提升三成 采用新架构", source="a.com")
    right = _candidate(2, "行业要闻三则", text="某公司发布新一代芯片 性能提升三成", source="b.com")
    assert cl.candidate_pairs([left, right], floor=0.08) == [(1, 2)]


# ── 合并 ──


def _ids(clusters: list[cl.Cluster]) -> list[list[int]]:
    return sorted(sorted(cluster.members) for cluster in clusters)


def test_merge_groups_same_and_follow_up() -> None:
    items = [_candidate(1, "a"), _candidate(2, "b"), _candidate(3, "c")]
    merged = cl._merge(items, {(1, 2): cl.REL_SAME, (1, 3): cl.REL_FOLLOW_UP})
    assert _ids(merged) == [[1, 2, 3]]


def test_merge_keeps_different_apart() -> None:
    items = [_candidate(1, "a"), _candidate(2, "b")]
    assert _ids(cl._merge(items, {(1, 2): cl.REL_DIFFERENT})) == [[1], [2]]


def test_merge_does_not_lose_the_representative_node() -> None:
    """并查集只把被 union 的一方写进 parent，代表节点只在 values 里。

    只遍历 `set(parent)` 会漏掉代表，整组被丢掉——`dedup.parse_groups` 上已踩过一次。
    这里用"两条合并 + 一条无关"锁住它：三条都必须出现在结果里。
    """
    items = [_candidate(1, "a"), _candidate(2, "b"), _candidate(3, "c")]
    merged = cl._merge(items, {(1, 2): cl.REL_SAME})
    assert _ids(merged) == [[1, 2], [3]]


def test_merge_without_relations_keeps_everything_separate() -> None:
    items = [_candidate(1, "a"), _candidate(2, "b")]
    assert _ids(cl._merge(items, {})) == [[1], [2]]


def test_effective_treats_a_missing_conclusion_as_merge() -> None:
    """个别配对没答上来时合并，交给复核去拆——漏合并会让同一件事裂成两条进日报。"""
    effective = cl._effective({(1, 2): cl.REL_SAME}, [(1, 2), (1, 3)])
    assert effective[(1, 3)] == cl.REL_SAME


def test_effective_keeps_an_explicit_different_apart() -> None:
    effective = cl._effective({(1, 2): cl.REL_DIFFERENT}, [(1, 2), (1, 3)])
    assert effective[(1, 2)] == cl.REL_DIFFERENT
    assert effective[(1, 3)] == cl.REL_SAME


def test_effective_merges_nothing_when_the_batch_has_no_conclusions() -> None:
    """整批没结论 = 模型这次不可用：把几十条资讯并成一件事比不合并糟得多。"""
    assert cl._effective({}, [(1, 2), (2, 3)]) == {}


# ── 角色 ──


def test_role_of_marks_earliest_as_primary() -> None:
    pairs = [cl.Pair(left=1, right=2, relation=cl.REL_FOLLOW_UP, certain=True)]
    roles = cl.role_of([1, 2], primary=1, pairs=pairs)
    assert roles[1] == cl.ROLE_PRIMARY
    assert roles[2] == cl.ROLE_FOLLOW_UP


def test_role_of_defaults_to_duplicate() -> None:
    roles = cl.role_of([1, 2], primary=1, pairs=[])
    assert roles[1] == cl.ROLE_PRIMARY
    assert roles[2] == cl.ROLE_DUPLICATE


def test_role_of_only_uses_pairs_that_were_confirmed() -> None:
    """没被模型确认的 follow_up 不算数，否则会把重复报道算成"新进展"并虚增热度。"""
    pairs = [cl.Pair(left=1, right=2, relation=cl.REL_FOLLOW_UP, certain=False)]
    assert cl.role_of([1, 2], primary=1, pairs=pairs)[2] == cl.ROLE_DUPLICATE


def test_pick_primary_prefers_the_earliest_published() -> None:
    old = _candidate(2, "首发")
    old = cl.Candidate(news_item_id=2, title="首发", published_at=NOW - timedelta(hours=10))
    new = cl.Candidate(news_item_id=1, title="深度报道", published_at=NOW - timedelta(hours=1))
    assert cl.pick_primary([new, old]) == 2, "一手报道按时间判定，不是取编号小或质量高的"


def test_pick_primary_falls_back_to_the_smallest_id_when_undated() -> None:
    left = cl.Candidate(news_item_id=7, title="a")
    right = cl.Candidate(news_item_id=3, title="b")
    assert cl.pick_primary([left, right]) == 3


# ── 参与者与社区来源 ──


def test_participant_never_empty() -> None:
    """空参与者会让多条来源被当成同一个未知主体，热度反而算不出来。"""
    blank = cl.Candidate(news_item_id=5, title="x")
    assert blank.participant == "unknown:5"


def test_participant_falls_back_to_host() -> None:
    item = cl.Candidate(news_item_id=5, title="x", url="https://www.qbitai.com/a")
    assert item.participant == "www.qbitai.com"


def test_is_community_matches_subdomain() -> None:
    assert cl.is_community("https://news.ycombinator.com/item?id=1", ["news.ycombinator.com"]) is True
    assert cl.is_community("https://www.qbitai.com/a", ["news.ycombinator.com"]) is False


# ── 端到端：判定 + 复核 ──


async def test_assign_merges_pairs_the_model_called_the_same() -> None:
    llm = FakeLLM(replies=[reply("1,2,same"), reply("")])
    selector = cl.Clusterer(llm, EditorialSection())
    items = [_candidate(1, "Qwen-Image 2.1 Turbo 发布"), _candidate(2, "阿里发布 qwen-image-2.1-turbo")]
    plan = await selector.assign(items)
    assert len(plan.clusters) == 1
    assert sorted(plan.clusters[0].members) == [1, 2]


async def test_assign_review_can_veto_a_merged_group() -> None:
    """复核是最后一道闸：它说不成立，就把不确定合并的部分拆回去。"""
    llm = FakeLLM(replies=[reply("1,2,same"), reply("1,no")])
    selector = cl.Clusterer(llm, EditorialSection())
    items = [_candidate(1, "Qwen-Image 2.1 Turbo 发布"), _candidate(2, "阿里发布 qwen-image-2.1-turbo")]
    plan = await selector.assign(items)
    assert plan.vetoed, "被否决的组要记录在案，便于事后查看"
    assert [cluster.members for cluster in plan.clusters] == [[1], [2]]


async def test_assign_falls_back_to_singletons_when_the_model_fails() -> None:
    """模型不可用时各自成事件：内容不会丢，只是失去了合并能力。"""

    class Boom(FakeLLM):
        async def complete(self, messages: Any, tools: Any = None, *, max_tokens: Any = None) -> Any:
            raise RuntimeError("模型挂了")

    selector = cl.Clusterer(Boom(), EditorialSection())
    items = [_candidate(1, "Qwen-Image 2.1 Turbo 发布"), _candidate(2, "阿里发布 qwen-image-2.1-turbo")]
    plan = await selector.assign(items)
    assert sorted(len(cluster.members) for cluster in plan.clusters) == [1, 1]


async def test_assign_keeps_unrelated_items_apart() -> None:
    llm = FakeLLM(replies=[reply(""), reply("")])
    selector = cl.Clusterer(llm, EditorialSection())
    items = [_candidate(1, "苹果定档秋季发布会"), _candidate(2, "三星减产存储芯片")]
    plan = await selector.assign(items)
    assert len(plan.clusters) == 2


async def test_assign_single_item_skips_the_model() -> None:
    llm = FakeLLM()
    plan = await cl.Clusterer(llm, EditorialSection()).assign([_candidate(1, "a")])
    assert [cluster.members for cluster in plan.clusters] == [[1]]
    assert llm.calls == [], "只有一条时不该花模型调用"


# ── 落库 ──


async def _seed(db: Database, *, title: str, url: str, hours: int = 1, source: str = "量子位") -> int:
    async with db.session() as session:
        item = NewsItem(
            url=url,
            url_hash=url,
            title=title,
            source=source,
            summary="性能提升三成",
            published_at=NOW - timedelta(hours=hours),
            fetched_at=NOW,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _plan(members: list[int], relations: list[cl.Pair] | None = None) -> cl.Plan:
    return cl.Plan(clusters=[cl.Cluster(members=members)], pairs=list(relations or []))


async def test_write_plan_creates_story_and_report_items(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cluster.db'}")
    await db.prepare()
    first = await _seed(db, title="首发", url="https://a.example/1", hours=5)
    second = await _seed(db, title="后续", url="https://b.example/2", hours=1)
    items = [
        cl.Candidate(news_item_id=first, title="首发", source="量子位"),
        cl.Candidate(news_item_id=second, title="后续", source="infoq"),
    ]
    pairs = [cl.Pair(left=first, right=second, relation=cl.REL_FOLLOW_UP, certain=True)]
    stats = await cl.write_plan(db, await _plan([first, second], pairs), items)
    assert stats["stories"] == 1
    assert stats["items"] == 2
    async with db.session() as session:
        rows = (await session.scalars(select(ReportItem).order_by(ReportItem.news_item_id))).all()
    roles = {row.news_item_id: row.role for row in rows}
    assert roles[first] == cl.ROLE_PRIMARY
    assert roles[second] == cl.ROLE_FOLLOW_UP
    assert {row.participant for row in rows} == {"量子位", "infoq"}
    await db.dispose()


async def test_write_plan_joins_an_existing_story(tmp_path: Any) -> None:
    """组里有成员已属于某个事件就并入它——按成员反查比按标题匹配可靠。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cluster2.db'}")
    await db.prepare()
    first = await _seed(db, title="首发", url="https://a.example/1")
    items = [cl.Candidate(news_item_id=first, title="首发", source="量子位")]
    await cl.write_plan(db, await _plan([first]), items)
    second = await _seed(db, title="后续", url="https://b.example/2")
    items.append(cl.Candidate(news_item_id=second, title="后续", source="infoq"))
    stats = await cl.write_plan(db, await _plan([first, second]), items)
    assert stats["stories"] == 0, "并入已有事件，不新建"
    assert stats["items"] == 1, "已归组的那条不重复写入"
    async with db.session() as session:
        stories = (await session.scalars(select(Story))).all()
    assert len(stories) == 1
    await db.dispose()


async def test_write_plan_never_touches_a_locked_story(tmp_path: Any) -> None:
    """人工改过的归属不允许被后续自动归组覆盖——否则人改了等于没改。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cluster3.db'}")
    await db.prepare()
    first = await _seed(db, title="首发", url="https://a.example/1")
    items = [cl.Candidate(news_item_id=first, title="首发", source="量子位")]
    await cl.write_plan(db, await _plan([first]), items)
    async with db.session() as session:
        story = await session.scalar(select(Story))
        assert story is not None
        story.locked = True
        await session.commit()
    stats = await cl.write_plan(db, await _plan([first]), items)
    assert stats["skipped_locked"] == 1
    assert stats["items"] == 0
    async with db.session() as session:
        assert len((await session.scalars(select(ReportItem))).all()) == 1, "没有重复写入"
    await db.dispose()


async def test_load_candidates_skips_already_grouped_items(tmp_path: Any) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cluster4.db'}")
    await db.prepare()
    first = await _seed(db, title="首发", url="https://a.example/1")
    second = await _seed(db, title="未归组", url="https://b.example/2")
    items = [cl.Candidate(news_item_id=first, title="首发", source="量子位")]
    await cl.write_plan(db, await _plan([first]), items)
    loaded = await cl.load_candidates(db, days=14, now=NOW)
    assert [item.news_item_id for item in loaded] == [second]
    await db.dispose()


async def test_load_candidates_uses_the_stored_summary(tmp_path: Any) -> None:
    """摘要入库是给聚簇用的：只凭标题判"是不是同一件事"会漏掉中译稿。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cluster5.db'}")
    await db.prepare()
    await _seed(db, title="标题看不出关联", url="https://a.example/1")
    loaded = await cl.load_candidates(db, days=14, now=NOW)
    assert loaded[0].text == "性能提升三成"
    await db.dispose()
