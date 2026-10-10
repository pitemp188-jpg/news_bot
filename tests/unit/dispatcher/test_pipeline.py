"""
模块: tests.unit.dispatcher.test_pipeline
职责: 校验流水线顺序、报告落库、会话追加与投递失败重试分类
依赖: dispatcher.pipeline, core.db
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.fakes.llm import FakeLLM, reply

from newsbot.agent.runner import Findings
from newsbot.core.db import Database
from newsbot.core.errors import RetriableError
from newsbot.core.models import LlmUsage, Report, Task
from newsbot.dispatcher.pipeline import Pipeline
from newsbot.dispatcher.session import SessionStore
from newsbot.result.dedup import Deduper, Item, StoryGrouper
from newsbot.result.report import ReportBuilder


@dataclass
class Built:
    content: str = "报告正文"
    sources: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = [{"title": "标题", "url": "https://example.com/a"}]


class FakeRunner:
    def __init__(self, findings: Any = "findings") -> None:
        self.findings = findings
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def run(self, query: str, history: list[dict[str, Any]]) -> Any:
        self.calls.append((query, history))
        return self.findings


class FakeReporter:
    def __init__(self, built: Built | None = None) -> None:
        self.built = built or Built()
        self.seen: list[Any] = []

    async def build(self, task: Task, findings: Any) -> Built:
        self.seen.append(findings)
        return self.built


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str, str]] = []
        self.report_ids: list[int | None] = []

    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        self.sent.append((platform, chat_id, text))
        self.report_ids.append(report_id)
        return self.ok


async def _make_db(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    await db.init()
    return db


async def _make_task(db: Database, **overrides: Any) -> int:
    payload = {"kind": "chat", "query": "今天 AI 新闻", "status": "running", "platform": "qqbot", "chat_id": "c1"}
    payload.update(overrides)
    async with db.session() as session:
        task = Task(**payload)  # type: ignore[arg-type]
        session.add(task)
        await session.commit()
        return task.id


async def test_full_chain_persists_and_delivers(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        runner, reporter, sender = FakeRunner(), FakeReporter(), FakeSender()
        sessions = SessionStore(db)
        pipeline = Pipeline(db, runner=runner, reporter=reporter, sender=sender, sessions=sessions)
        task_id = await _make_task(db)

        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            content = await pipeline.execute(session, task)

        assert content == "报告正文"
        assert sender.sent == [("qqbot", "c1", "报告正文")]
        assert reporter.seen == ["findings"]
        async with db.session() as session:
            report = (await session.execute(select(Report))).scalar_one()
        assert report.task_id == task_id
        assert report.sources == [{"title": "标题", "url": "https://example.com/a"}]
        assert await sessions.load("qqbot", "c1") == [
            {"role": "user", "content": "今天 AI 新闻"},
            {"role": "assistant", "content": "报告正文"},
        ]
    finally:
        await db.dispose()


async def test_dedup_keeps_source_labels_aligned(tmp_path: Path) -> None:
    """去重剔除来源后，正文引用的编号仍要指向同一条来源（回归用例）。

    实测缺陷：编号曾与 sources 分成两个平行列表，去重把 14 条删到 2 条后编号
    整体前移，正文的 [S2] 指到了另一篇文章上。编号现在存在来源自身里。

    用 scheduled 是必须的：按历史剔除只对定时推送生效（手动提问是用户当下想看，
    不该因为"已推送过"就不给来源）。
    """
    db = await _make_db(tmp_path)
    try:
        # 正文只引用新的那条：被引用的编号有保护集，不会因为"已推送过"被剔除
        findings = Findings(
            answer="结论 [S14]",
            sources=[
                {"title": "旧的", "url": "https://example.com/dup", "label": "S9"},
                {"title": "新的", "url": "https://example.com/new", "label": "S14"},
            ],
        )
        deduper = Deduper()
        deduper.remember(Item(title="旧的", url="https://example.com/dup"))

        async def fresh() -> Deduper:
            return deduper

        pipeline = Pipeline(
            db,
            runner=FakeRunner(findings=findings),
            reporter=ReportBuilder(),
            sender=FakeSender(),
            sessions=SessionStore(db),
            deduper=fresh,
        )
        task_id = await _make_task(db, kind="scheduled")
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            content = await pipeline.execute(session, task)

        # 关键断言是"编号没有前移"：S14 仍叫 S14，不会被重编成 S1
        assert "S14. 新的 https://example.com/new" in content
        # 与历史重复且未被正文引用的来源不再列出
        assert "S9. " not in content
    finally:
        await db.dispose()


async def test_semantic_grouping_merges_rewrites_across_languages(tmp_path: Path) -> None:
    """中文改写对英文原稿：词法判据抓不到，靠模型的分组合并。"""
    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论 [S1] [S3]",
            sources=[
                {
                    "title": "Musk 与 Ambani 就 Starlink 印度牌照公开互怼",
                    "url": "https://cn.example/1",
                    "snippet": "两人在社交平台上互相指责，焦点是落地许可与频谱分配。",
                    "label": "S1",
                },
                {
                    "title": "Elon Musk intensifies attack on Ambani over Starlink India licence",
                    "url": "https://en.example/2",
                    "snippet": "The two traded barbs over the licence and spectrum allocation.",
                    "label": "S2",
                },
                {
                    "title": "英伟达发布新一代推理芯片",
                    "url": "https://news.example/3",
                    "snippet": "面向数据中心的推理加速卡。",
                    "label": "S3",
                },
                {"title": "某车企公布三季度交付量", "url": "https://news.example/4", "label": "S4"},
            ],
        )
        llm = FakeLLM(replies=[reply("1,2", tokens=9)])

        async def fixed() -> Deduper:
            return Deduper(grouper=StoryGrouper(llm, min_items=4))

        pipeline = Pipeline(
            db,
            runner=FakeRunner(findings=findings),
            reporter=ReportBuilder(),
            sender=FakeSender(),
            sessions=SessionStore(db),
            deduper=fixed,
        )
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            await pipeline.execute(session, task)

        assert [item["label"] for item in findings.sources] == ["S1", "S3", "S4"]

        # 语义分组也是一次模型调用，用量必须落库，否则后台成本统计会漏
        async with db.session() as session:
            usages = list((await session.execute(select(LlmUsage))).scalars())
        assert [usage.prompt_tokens for usage in usages] == [9]
    finally:
        await db.dispose()


async def test_model_usage_is_persisted(tmp_path: Path) -> None:
    """每次模型调用的用量都要落库，否则后台的成本统计永远是 0（回归用例）。"""
    from newsbot.agent.runner import Findings
    from newsbot.core.llm import Usage
    from newsbot.core.models import LlmUsage

    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论",
            usages=[
                Usage(prompt_tokens=120, completion_tokens=30, model="deepseek-v4-flash"),
                Usage(prompt_tokens=200, completion_tokens=80, model="deepseek-v4-flash"),
            ],
        )
        pipeline = Pipeline(
            db, runner=FakeRunner(findings), reporter=FakeReporter(), sender=FakeSender(), sessions=SessionStore(db)
        )
        task_id = await _make_task(db)
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            await pipeline.execute(session, task)

        async with db.session() as session:
            rows = (await session.execute(select(LlmUsage).order_by(LlmUsage.id))).scalars().all()
        assert [(row.prompt_tokens, row.completion_tokens) for row in rows] == [(120, 30), (200, 80)]
        assert {row.model for row in rows} == {"deepseek-v4-flash"}
        assert {row.task_id for row in rows} == {task_id}
    finally:
        await db.dispose()


async def test_runner_without_usage_writes_nothing(tmp_path: Path) -> None:
    """没有用量信息时不要写空记录。"""
    from newsbot.core.models import LlmUsage

    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(
            db, runner=FakeRunner("findings"), reporter=FakeReporter(), sender=FakeSender(), sessions=SessionStore(db)
        )
        task_id = await _make_task(db)
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            await pipeline.execute(session, task)
        async with db.session() as session:
            assert list((await session.execute(select(LlmUsage))).scalars()) == []
    finally:
        await db.dispose()


async def test_history_is_passed_to_runner(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        runner = FakeRunner()
        sessions = SessionStore(db)
        await sessions.append("qqbot", "c1", user_text="上一问", reply_text="上一答")
        pipeline = Pipeline(db, runner=runner, reporter=FakeReporter(), sender=FakeSender(), sessions=sessions)

        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            await pipeline.execute(session, task)

        assert runner.calls[0][1][0]["content"] == "上一问"
    finally:
        await db.dispose()


async def test_delivery_failure_is_retriable(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(
            db,
            runner=FakeRunner(),
            reporter=FakeReporter(),
            sender=FakeSender(ok=False),
            sessions=SessionStore(db),
        )
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            with pytest.raises(RetriableError):
                await pipeline.execute(session, task)
        # 报告在投递前已提交（避免网络 IO 期间持有写事务），但会话历史不追加
        async with db.session() as session:
            assert len((await session.execute(select(Report))).scalars().all()) == 1
        assert await SessionStore(db).load("qqbot", "c1") == []
    finally:
        await db.dispose()


async def test_missing_sender_is_retriable(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        pipeline = Pipeline(db, runner=FakeRunner(), reporter=FakeReporter(), sender=None, sessions=SessionStore(db))
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            with pytest.raises(RetriableError):
                await pipeline.execute(session, task)
    finally:
        await db.dispose()


async def test_task_without_target_only_reports(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    try:
        sender = FakeSender()
        pipeline = Pipeline(db, runner=FakeRunner(), reporter=FakeReporter(), sender=sender, sessions=SessionStore(db))
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db, platform=None, chat_id=None))
            assert task is not None
            await pipeline.execute(session, task)
        assert sender.sent == []
        async with db.session() as session:
            assert len((await session.execute(select(Report))).scalars().all()) == 1
    finally:
        await db.dispose()


async def test_same_story_is_merged_keeping_the_richest_source(tmp_path: Path) -> None:
    """同一件事的多家报道只留质量最高的一条：这是"内容去重"而不是"按来源去重"。"""
    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论 [S2] [S3]",
            sources=[
                {
                    "title": "某公司发布 Qwen-Image-2.1-Turbo",
                    "url": "https://wire.example/1",
                    "snippet": "模型已开源。",
                    "weight": 0.7,
                    "label": "S1",
                },
                {
                    "title": "某公司发布 Qwen-Image-2.1-Turbo，支持中文渲染",
                    "url": "https://official.example/2",
                    "snippet": "Qwen-Image-2.1-Turbo 是 8 步出图的 7B 图像模型，已在 ModelScope 开源，支持中文渲染。",
                    "weight": 1.2,
                    "label": "S2",
                },
                {
                    "title": "英伟达发布新一代推理芯片",
                    "url": "https://news.example/3",
                    "snippet": "面向数据中心的推理加速卡。",
                    "label": "S3",
                },
            ],
        )

        async def fresh() -> Deduper:
            return Deduper()

        pipeline = Pipeline(
            db,
            runner=FakeRunner(findings=findings),
            reporter=ReportBuilder(),
            sender=FakeSender(),
            sessions=SessionStore(db),
            deduper=fresh,
        )
        task_id = await _make_task(db)
        async with db.session() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            await pipeline.execute(session, task)

        kept = [item["label"] for item in findings.sources]
        assert kept == ["S2", "S3"], "同一件事留下信息最全的那条；另一件事不受影响"
    finally:
        await db.dispose()


async def test_cited_labels_are_never_dropped(tmp_path: Path) -> None:
    """正文引用过的编号一律保留——引用悬空是硬规则禁止的（回归用例）。

    实测缺陷：正文写了 `[S1] [S11] [S16] [S17] [S20]`，而去重把 S1/S11/S16 剔除，
    来源列表里只剩 S17/S20，读者无法核对其中三条。根因是按历史剔除对手动提问也
    生效（问的是"刚看过的同一话题"，于是整批来源被当成"已推送过"）。现在两层保证：
    手动提问不做历史剔除，且正文引用过的编号有保护集兜底。
    """
    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论 [S31] [S32]",
            sources=[
                {"title": "同一件事的简讯", "url": "https://short.example/1", "snippet": "简讯。", "label": "S31"},
                {
                    "title": "同一件事的详版",
                    "url": "https://long.example/2",
                    "snippet": "Qwen-Image-2.1-Turbo 是 8 步出图的 7B 图像模型，已在 ModelScope 开源。",
                    "label": "S32",
                },
            ],
        )
        # 模型明确说这两条是同一件事，但正文同时引用了两个编号
        llm = FakeLLM(replies=[reply("1,2")])

        async def fresh() -> Deduper:
            return Deduper(grouper=StoryGrouper(llm, min_items=2))

        pipeline = Pipeline(
            db,
            runner=FakeRunner(findings=findings),
            reporter=ReportBuilder(),
            sender=FakeSender(),
            sessions=SessionStore(db),
            deduper=fresh,
        )
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            content = await pipeline.execute(session, task)

        labels = [item["label"] for item in findings.sources]
        assert labels == ["S31", "S32"], f"被正文引用的编号不能剔除，实际保留 {labels}"
        for label in re.findall(r"\[S(\d+)\]", findings.answer):
            assert f"S{label}. " in content, f"正文引用了 S{label}，但来源列表里没有它"
    finally:
        await db.dispose()


async def test_uncited_duplicate_is_still_merged(tmp_path: Path) -> None:
    """保护集只保护被引用的：没被正文引用的重复来源照常合并（否则去重就废了）。"""
    db = await _make_db(tmp_path)
    try:
        findings = Findings(
            answer="结论 [S32]",
            sources=[
                {"title": "同一件事的简讯", "url": "https://short.example/1", "snippet": "简讯。", "label": "S31"},
                {
                    "title": "同一件事的详版",
                    "url": "https://long.example/2",
                    "snippet": "Qwen-Image-2.1-Turbo 是 8 步出图的 7B 图像模型，已在 ModelScope 开源。",
                    "label": "S32",
                },
            ],
        )
        llm = FakeLLM(replies=[reply("1,2")])

        async def fresh() -> Deduper:
            return Deduper(grouper=StoryGrouper(llm, min_items=2))

        pipeline = Pipeline(
            db,
            runner=FakeRunner(findings=findings),
            reporter=ReportBuilder(),
            sender=FakeSender(),
            sessions=SessionStore(db),
            deduper=fresh,
        )
        async with db.session() as session:
            task = await session.get(Task, await _make_task(db))
            assert task is not None
            await pipeline.execute(session, task)

        assert [item["label"] for item in findings.sources] == ["S32"]
    finally:
        await db.dispose()
