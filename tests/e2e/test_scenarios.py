"""
模块: tests.e2e.test_scenarios
职责: 端到端场景——每晚定时推送、聊天指令查询、运维主动推送
依赖: newsbot.app, tests.fakes
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.search import FakeSearchTool

from newsbot.agent.tools.fetch import FetchTool
from newsbot.app import App
from newsbot.core.config import AgentSection, Config, ScheduleSection, Secrets
from newsbot.core.llm import ToolCall
from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task

pytestmark = pytest.mark.e2e

TERMINAL = {"succeeded", "failed", "timeout", "cancelled"}


def _config(tmp_path: Path) -> Config:
    return Config(
        secrets=Secrets(_env_file=None, llm_api_key="sk-test", weixin_allowed_users="u1"),
        schedule=ScheduleSection(default_time="21:00", default_topics=["AI"], dedup_days=7),
        data_dir=tmp_path,
    )


async def _wait_tasks(app: App, count: int, timeout: float = 20.0) -> list[Task]:
    """等待指定数量的任务全部结束。"""
    assert app.db is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    tasks: list[Task] = []
    while loop.time() < deadline:
        async with app.db.session() as session:
            tasks = list((await session.execute(select(Task).order_by(Task.id))).scalars())
        if len(tasks) >= count and all(task.status in TERMINAL for task in tasks):
            return tasks
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未结束: {[(task.id, task.status) for task in tasks]}")


async def _build_app(tmp_path: Path, url: str, *, rounds: int = 1) -> tuple[App, FakeAdapter]:
    """组装真实服务，仅把联网搜索与模型换成替身；抓取走本地站点。"""
    adapter = FakeAdapter(platform="weixin", max_length=2000)
    script: list = []
    for index in range(rounds):
        script.append(reply("", calls=[ToolCall(id=f"s{index}", name="search", arguments={"query": "AI 最新进展"})]))
        script.append(reply("", calls=[ToolCall(id=f"f{index}", name="fetch", arguments={"url": url})]))
        script.append(reply("推理成本较上代下降约三成 [1]"))
    llm = FakeLLM(replies=script)
    app = App(
        _config(tmp_path),
        llm=llm,
        adapters=[adapter],
        tools=[
            FakeSearchTool(hits=[{"title": "示例资讯：AI 行业周报", "url": url, "snippet": "摘要"}]),
            FetchTool(AgentSection(), allow_private=True),
        ],
    )
    await app.start()
    return app, adapter


async def test_scenario_scheduled_push(tmp_path: Path, local_server: str) -> None:
    """场景一：到点自动推送——首次对话建立订阅，触发后自动采集并投递。"""
    url = f"{local_server}/article.html"
    app, adapter = await _build_app(tmp_path, url)
    try:
        await adapter.emit("/帮助", chat_id="u1", user_id="u1")
        assert app.db is not None and app.scheduler is not None
        async with app.db.session() as session:
            schedule = (await session.execute(select(Schedule))).scalar_one()
        assert schedule.cron == "0 21 * * *"

        await app.scheduler.run_job(schedule.id)
        tasks = await _wait_tasks(app, 1)
        assert tasks[0].kind == "scheduled" and tasks[0].status == "succeeded"

        pushed = [text for _chat, text, _reply in adapter.sent if "下降约三成" in text]
        assert pushed, adapter.sent
        assert "article.html" in pushed[0]

        async with app.db.session() as session:
            report = (await session.execute(select(Report))).scalar_one()
            deliveries = list((await session.execute(select(Delivery))).scalars())
            item = (await session.execute(select(NewsItem))).scalar_one()
        # 回执与报告各有一条投递记录，报告对应那条必须已投递成功
        linked = [row for row in deliveries if row.report_id == report.id]
        assert len(linked) == 1 and linked[0].status == "sent"
        assert linked[0].content and "下降约三成" in linked[0].content
        assert item.url.endswith("/article.html")
    finally:
        await app.stop()


async def test_scenario_chat_query(tmp_path: Path, local_server: str) -> None:
    """场景二：聊天里直接提问——先回执，再异步投递结果；同一来源不重复入库。

    **手动提问不按历史去重**：用户当下在问，把来源当成"已推送过"而不给他，是错的
    （实测同一话题连问两次，39 条来源被删到 9 条，正文引用的 5 个编号里 3 个消失）。
    这里断言连问两次仍能拿到完整回答，且资讯库里不会重复入库。
    """
    url = f"{local_server}/article.html"
    app, adapter = await _build_app(tmp_path, url, rounds=2)
    try:
        await adapter.emit("最近 AI 有什么新进展", chat_id="u1", user_id="u1")
        tasks = await _wait_tasks(app, 1)
        assert tasks[0].kind == "chat" and tasks[0].status == "succeeded"
        assert adapter.sent[0][1].startswith("已收到，正在搜索")
        assert any("下降约三成" in text for _chat, text, _reply in adapter.sent)

        await adapter.emit("再查一次 AI", chat_id="u1", user_id="u1")
        await _wait_tasks(app, 2)
        second = [text for _chat, text, _reply in adapter.sent[2:]]
        assert second, adapter.sent
        assert any("下降约三成" in text for text in second)
        # 来源要列出来，不能被当成"已推送过"删掉
        assert all("article.html" in text for text in second if "来源：" in text)
        async with app.db.session() as session:
            assert len(list((await session.execute(select(NewsItem))).scalars())) == 1
    finally:
        await app.stop()


async def test_scenario_notify_reuses_delivery(tmp_path: Path, local_server: str) -> None:
    """场景补充：运维主动推送（notify）复用同一投递通道。"""
    url = f"{local_server}/article.html"
    app, adapter = await _build_app(tmp_path, url)
    try:
        assert await app.notify("服务已重启") is False  # 还没有订阅目标
        await adapter.emit("/帮助", chat_id="u1", user_id="u1")
        adapter.sent.clear()
        assert await app.notify("服务已重启") is True
        assert adapter.sent[-1][1] == "服务已重启"
    finally:
        await app.stop()
