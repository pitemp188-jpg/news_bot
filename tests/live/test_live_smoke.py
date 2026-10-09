"""
模块: tests.live.test_live_smoke
职责: 整机冒烟——真实组装的服务 + 真实模型 + 真实抓取，跑全部聊天指令与两种推送场景
依赖: newsbot.app, tests.fakes

平台侧用 FakeAdapter 顶替（微信 / QQ 需要真实凭证，见 T2.3 / T2.4 的 live 测试），
除此之外的每一环都是真实实现：数据库、队列、调度、指令、路由投递、模型、抓取、成稿、去重。

运行：`uv run python -m pytest -m live tests/live`
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.agent.tools.fetch import FetchTool
from newsbot.app import App
from newsbot.core.config import AgentSection, load_config
from newsbot.core.llm import OpenAIClient
from newsbot.core.models import Delivery, NewsItem, Report, Schedule, Task

pytestmark = pytest.mark.live

USER = "u1"
STABLE_URL = "https://example.com/"
STABLE_TITLE = "Example Domain"
TERMINAL = {"succeeded", "failed", "timeout", "cancelled"}


class FixedSearch:
    """固定命中列表的搜索替身：真实抓取交给 FetchTool，避免依赖外部搜索服务。"""

    name = "search"
    description = "返回候选来源列表；只会返回下面这些网址，其他网址都不要抓。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索关键词"}},
        "required": ["query"],
    }

    def __init__(self, hits: list[dict[str, str]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def run(self, query: str = "", **_: Any) -> ToolResult:
        self.queries.append(query)
        lines = [
            f"{index}. {hit['title']}\n   {hit['url']}\n   摘要：{hit.get('snippet', '')}"
            for index, hit in enumerate(self._hits, 1)
        ]
        return ToolResult(
            text="\n".join(lines), sources=[Source(title=hit["title"], url=hit["url"]) for hit in self._hits]
        )

    async def aclose(self) -> None:
        return None


def _config(tmp_path):
    config = load_config()
    if not config.secrets.llm_api_key:
        pytest.skip("未配置 LLM_API_KEY，跳过 live 测试")
    config.data_dir = tmp_path
    config.secrets.weixin_allowed_users = USER
    # 收紧预算，避免真实模型在公网不稳定时长时间重试
    config.agent = AgentSection(max_steps=5, max_tokens=30_000)
    config.queue.task_timeout_seconds = 300
    return config


async def _wait_tasks(app: App, count: int, timeout: float = 240.0) -> list[Task]:
    assert app.db is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    tasks: list[Task] = []
    while loop.time() < deadline:
        async with app.db.session() as session:
            tasks = list((await session.execute(select(Task).order_by(Task.id))).scalars())
        if len(tasks) >= count and all(task.status in TERMINAL for task in tasks):
            return tasks
        await asyncio.sleep(0.3)
    raise AssertionError(f"任务未结束: {[(task.id, task.status, task.error) for task in tasks]}")


async def _replies(adapter: FakeAdapter, timeout: float = 60.0) -> None:
    """等所有异步投递落地，避免断言时内容还没发出来。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = -1
    while loop.time() < deadline:
        if len(adapter.sent) == last:
            return
        last = len(adapter.sent)
        await asyncio.sleep(0.4)


@pytest.fixture
async def live_app(tmp_path):
    """真实组装的服务，仅平台适配器与搜索引擎用替身；模型与抓取都是真的。"""
    config = _config(tmp_path)
    client = OpenAIClient(config.secrets, max_retries=2)
    adapter = FakeAdapter(platform="weixin", max_length=2000)
    app = App(
        config,
        llm=client,
        adapters=[adapter],
        tools=[
            FixedSearch([{"title": STABLE_TITLE, "url": STABLE_URL, "snippet": "这是一个用于文档示例的保留域名。"}]),
            FetchTool(config.agent, allow_private=False),
        ],
    )
    await app.start()
    try:
        yield app, adapter
    finally:
        await app.stop()
        await client.aclose()


async def test_live_smoke_all_chat_commands(live_app) -> None:
    """全部聊天指令在真实链路上可用，且回复真实投递到平台。"""
    _app, adapter = live_app

    await adapter.emit("/帮助", chat_id=USER, user_id=USER)
    await _replies(adapter)
    assert any("可用指令" in text for _chat, text, _ in adapter.sent), adapter.sent

    # 未授权用户不该产生任何回复
    before = len(adapter.sent)
    await adapter.emit("/帮助", chat_id="stranger", user_id="stranger")
    await asyncio.sleep(0.3)
    assert len(adapter.sent) == before

    await adapter.emit("/订阅 半导体 08:30", chat_id=USER, user_id=USER)
    await adapter.emit("/退订", chat_id=USER, user_id=USER)
    await _replies(adapter)
    listing = adapter.sent[-1][1]
    assert "半导体" in listing, adapter.sent[-3:]


async def test_live_smoke_chat_query_and_schedule(live_app) -> None:
    """场景 S2：真实提问 → 回执 → 真实采集 → 投递；场景 S1：到点自动推送。"""
    app, adapter = live_app

    await adapter.emit("example.com 这个站点是做什么的", chat_id=USER, user_id=USER)
    await _replies(adapter)
    assert adapter.sent[0][1].startswith("已收到，正在搜索"), adapter.sent[:2]

    tasks = await _wait_tasks(app, 1)
    assert tasks[0].kind == "chat" and tasks[0].status == "succeeded", tasks[0].error

    await _replies(adapter)
    body = "\n".join(text for _chat, text, _ in adapter.sent)
    assert "来源：" in body and "example.com" in body, body

    assert app.db is not None and app.scheduler is not None
    async with app.db.session() as session:
        schedule = (
            await session.execute(select(Schedule).where(Schedule.platform == "weixin", Schedule.chat_id == USER))
        ).scalar_one()
        report = (await session.execute(select(Report))).scalar_one()
        news = (await session.execute(select(NewsItem))).scalars().all()
    assert report.content and news and news[0].url == STABLE_URL

    # 场景 S1：定时触发；来源已在资讯库中，去重应生效
    adapter.sent.clear()
    await app.scheduler.run_job(schedule.id)
    tasks = await _wait_tasks(app, 2)
    assert tasks[1].kind == "scheduled" and tasks[1].status == "succeeded", tasks[1].error
    await _replies(adapter)
    pushed = "\n".join(text for _chat, text, _ in adapter.sent)
    assert pushed.strip(), "定时任务没有推送任何内容"
    assert "已去重" in pushed or STABLE_URL.rstrip("/") in pushed, pushed

    async with app.db.session() as session:
        deliveries = list((await session.execute(select(Delivery))).scalars())
    assert {row.status for row in deliveries} == {"sent"}


async def test_live_smoke_notify_reaches_chat(live_app) -> None:
    """运维推送：notify 复用同一投递通道，且能落到已订阅会话。"""
    app, adapter = live_app
    assert await app.notify("冒烟测试完成") is False  # 尚无订阅目标

    await adapter.emit("/帮助", chat_id=USER, user_id=USER)
    await _replies(adapter)
    adapter.sent.clear()
    assert await app.notify("冒烟测试完成") is True
    assert adapter.sent[-1] == (USER, "冒烟测试完成", None)
