"""
模块: tests.unit.api.routes.test_tasks
职责: 校验任务接口——列表过滤、详情、手动提交、取消与重跑
依赖: newsbot.api.routes.tasks
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from newsbot.core.models import Task

TERMINAL = {"succeeded", "failed", "timeout", "cancelled"}


async def _wait_finished(api, task_id: int, timeout: float = 10.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = ""
    while loop.time() < deadline:
        async with api.app.db.session() as session:
            row = await session.get(Task, task_id)
            status = row.status if row else ""
        if status in TERMINAL:
            return status
        await asyncio.sleep(0.02)
    return status


async def test_list_empty(api) -> None:
    await api.login()
    body = (await api.get("/api/tasks")).json()
    assert body == {"total": 0, "items": []}


async def test_submit_task_runs_and_filters(api) -> None:
    await api.login()
    created = await api.write(
        "post", "/api/tasks", json={"query": "最近的 AI 进展", "platform": "weixin", "chat_id": "u1"}
    )
    assert created.status_code == 201
    task = created.json()
    assert task["kind"] == "manual" and task["status"] == "pending"

    assert await _wait_finished(api, task["id"]) == "succeeded"

    body = (await api.get("/api/tasks")).json()
    assert body["total"] == 1
    assert body["items"][0]["query"] == "最近的 AI 进展"

    succeeded = (await api.get("/api/tasks?status=succeeded")).json()
    assert succeeded["total"] == 1
    failed = (await api.get("/api/tasks?status=failed")).json()
    assert failed["total"] == 0
    chat = (await api.get("/api/tasks?kind=chat")).json()
    assert chat["total"] == 0


async def test_task_detail_includes_report_and_delivery(api) -> None:
    await api.login()
    task = (
        await api.write("post", "/api/tasks", json={"query": "查一下", "platform": "weixin", "chat_id": "u1"})
    ).json()
    await _wait_finished(api, task["id"])

    body = (await api.get(f"/api/tasks/{task['id']}")).json()
    assert body["task"]["id"] == task["id"]
    assert len(body["reports"]) == 1
    assert body["reports"][0]["content"]
    assert [item["status"] for item in body["deliveries"]] == ["sent"]
    assert body["deliveries"][0]["platform"] == "weixin"


async def test_task_detail_missing(api) -> None:
    await api.login()
    response = await api.get("/api/tasks/99999")
    assert response.status_code == 404


async def test_submit_requires_query(api) -> None:
    await api.login()
    assert (await api.write("post", "/api/tasks", json={"query": ""})).status_code == 422


async def test_cancel_running_task(api) -> None:
    await api.login()
    # 用不存在的任务验证 404，再用真实任务验证取消后的状态流转
    assert (await api.write("post", "/api/tasks/99999/cancel")).status_code == 404

    async def _slow(_session, _task) -> str | None:
        await asyncio.sleep(5)
        return "太慢了"

    assert api.app.queue is not None
    api.app.queue.set_executor(_slow)
    task = (await api.write("post", "/api/tasks", json={"query": "慢任务"})).json()
    await asyncio.sleep(0.05)

    cancelled = await api.write("post", f"/api/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # 已经结束的任务不能再取消
    assert (await api.write("post", f"/api/tasks/{task['id']}/cancel")).status_code == 409

    async with api.app.db.session() as session:
        rows = (await session.execute(select(Task))).scalars().all()
    assert [row.status for row in rows] == ["cancelled"]


async def test_retry_finished_task_requeues(api) -> None:
    await api.login()
    assert (await api.write("post", "/api/tasks/99999/retry")).status_code == 404

    task = (await api.write("post", "/api/tasks", json={"query": "要重跑的"})).json()
    await _wait_finished(api, task["id"])

    retried = await api.write("post", f"/api/tasks/{task['id']}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "pending" and body["attempts"] == 0 and body["error"] is None

    assert await _wait_finished(api, task["id"]) == "succeeded"


async def test_retry_rejects_running_task(api) -> None:
    await api.login()

    async def _slow(_session, _task) -> str | None:
        await asyncio.sleep(5)
        return "慢"

    assert api.app.queue is not None
    api.app.queue.set_executor(_slow)
    task = (await api.write("post", "/api/tasks", json={"query": "运行中"})).json()
    await asyncio.sleep(0.05)
    assert (await api.write("post", f"/api/tasks/{task['id']}/retry")).status_code == 409
    await api.write("post", f"/api/tasks/{task['id']}/cancel")
