"""
模块: tests.unit.api.routes.test_system
职责: 校验系统接口——运行状态计数与主动推送
依赖: newsbot.api.routes.system
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from newsbot.core.models import Delivery, Task

TASK_STATES = {"pending", "running", "succeeded", "failed", "timeout", "cancelled"}


async def test_status_reports_runtime_state(api) -> None:
    await api.login()
    body = (await api.get("/api/system/status")).json()
    assert body["platforms"] == {"weixin": True}
    assert body["queue"] == {"depth": 0, "running": 0}
    assert body["timezone"] == "Asia/Shanghai"
    assert body["today_tokens"] == {"prompt": 0, "completion": 0}
    assert body["running"] == {"deliveries": 0, "waiting_user": 0}
    assert body["content"] == {"news": 0, "reports": 0, "schedules": 0}
    # 全部状态都要有键，前端才能稳定渲染卡片
    assert set(body["tasks"]) == TASK_STATES
    assert all(value == 0 for value in body["tasks"].values())


async def test_status_counts_after_activity(api) -> None:
    await api.login()
    await api.write("post", "/api/tasks", json={"query": "跑一次", "platform": "weixin", "chat_id": "u1"})
    await api.write(
        "post", "/api/schedules", json={"topics": ["AI"], "time": "21:00", "platform": "weixin", "chat_id": "u1"}
    )

    # 投递记录先于任务终态写入，等任务真正结束再断言
    for _ in range(300):
        async with api.app.db.session() as session:
            task = (await session.execute(select(Task))).scalars().one()
            deliveries = (await session.execute(select(Delivery))).scalars().all()
        if task.status == "succeeded" and deliveries:
            break
        await asyncio.sleep(0.02)

    body = (await api.get("/api/system/status")).json()
    assert body["content"]["schedules"] == 1
    assert body["content"]["reports"] == 1
    assert body["running"]["deliveries"] >= 1
    assert body["tasks"]["succeeded"] == 1


async def test_notify_pushes_to_subscribed_chat(api) -> None:
    await api.login()
    # 没有任何订阅时没有目标可推
    assert (await api.write("post", "/api/system/notify", json={"text": "上线了"})).status_code == 502

    await api.write(
        "post", "/api/schedules", json={"topics": ["AI"], "time": "21:00", "platform": "weixin", "chat_id": "u1"}
    )
    api.adapter.sent.clear()
    response = await api.write("post", "/api/system/notify", json={"text": "上线了"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert api.adapter.sent[-1] == ("u1", "上线了", None)

    assert (await api.write("post", "/api/system/notify", json={"text": ""})).status_code == 422
