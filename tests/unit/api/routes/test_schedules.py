"""
模块: tests.unit.api.routes.test_schedules
职责: 校验定时任务接口——新建、改时间改主题、启停、删除与调度器同步
依赖: newsbot.api.routes.schedules
"""

from __future__ import annotations

from newsbot.dispatcher.scheduler import REFRESH_JOB_ID


def _schedule_jobs(app) -> set[str]:
    """订阅作业集合。事件层刷新作业与订阅无关、启动即挂上，故先剔除。"""
    assert app.scheduler is not None
    return app.scheduler.job_ids() - {REFRESH_JOB_ID}


async def test_list_empty(api) -> None:
    await api.login()
    assert (await api.get("/api/schedules")).json() == {"items": []}


async def test_create_with_time_and_cron(api) -> None:
    await api.login()
    by_time = await api.write(
        "post", "/api/schedules", json={"topics": ["半导体"], "time": "08:30", "platform": "weixin", "chat_id": "u1"}
    )
    assert by_time.status_code == 201
    assert by_time.json()["cron"] == "30 8 * * *"

    by_cron = await api.write(
        "post",
        "/api/schedules",
        json={"topics": ["AI", "算力"], "cron": "0 9 * * 1", "platform": "qqbot", "chat_id": "g1"},
    )
    assert by_cron.status_code == 201
    assert by_cron.json()["topics"] == ["AI", "算力"]

    items = (await api.get("/api/schedules")).json()["items"]
    assert [item["topics"][0] for item in items] == ["半导体", "AI"]

    # 新建后调度器立即同步，不必重启
    assert _schedule_jobs(api.app) == {f"schedule-{item['id']}" for item in items}


async def test_create_validation(api) -> None:
    await api.login()
    bad_time = await api.write(
        "post", "/api/schedules", json={"topics": ["AI"], "time": "25:00", "platform": "w", "chat_id": "c"}
    )
    assert bad_time.status_code == 422 and "HH:MM" in bad_time.json()["detail"]

    bad_cron = await api.write(
        "post", "/api/schedules", json={"topics": ["AI"], "cron": "* * *", "platform": "w", "chat_id": "c"}
    )
    assert bad_cron.status_code == 422 and "五段" in bad_cron.json()["detail"]

    blank_topic = await api.write(
        "post", "/api/schedules", json={"topics": ["   "], "time": "09:00", "platform": "w", "chat_id": "c"}
    )
    assert blank_topic.status_code == 422

    too_many = await api.write(
        "post",
        "/api/schedules",
        json={"topics": ["a", "b", "c", "d", "e", "f"], "time": "09:00", "platform": "w", "chat_id": "c"},
    )
    assert too_many.status_code == 422


async def test_update_topics_time_and_enabled(api) -> None:
    await api.login()
    created = (
        await api.write(
            "post", "/api/schedules", json={"topics": ["AI"], "time": "21:00", "platform": "weixin", "chat_id": "u1"}
        )
    ).json()

    renamed = await api.write("patch", f"/api/schedules/{created['id']}", json={"topics": ["新能源"], "time": "07:05"})
    assert renamed.status_code == 200
    assert renamed.json()["topics"] == ["新能源"]
    assert renamed.json()["cron"] == "5 7 * * *"

    disabled = await api.write("patch", f"/api/schedules/{created['id']}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    assert _schedule_jobs(api.app) == set()

    enabled = await api.write("patch", f"/api/schedules/{created['id']}", json={"enabled": True})
    assert enabled.json()["enabled"] is True
    assert _schedule_jobs(api.app) == {f"schedule-{created['id']}"}

    assert (await api.write("patch", "/api/schedules/99999", json={"enabled": False})).status_code == 404


async def test_update_rejects_bad_input(api) -> None:
    await api.login()
    created = (
        await api.write(
            "post", "/api/schedules", json={"topics": ["AI"], "time": "21:00", "platform": "weixin", "chat_id": "u1"}
        )
    ).json()
    assert (await api.write("patch", f"/api/schedules/{created['id']}", json={"time": "99:99"})).status_code == 422
    assert (await api.write("patch", f"/api/schedules/{created['id']}", json={"topics": []})).status_code == 422


async def test_delete_disables_by_default_and_purges_on_demand(api) -> None:
    await api.login()
    first = (
        await api.write(
            "post", "/api/schedules", json={"topics": ["AI"], "time": "21:00", "platform": "weixin", "chat_id": "u1"}
        )
    ).json()
    second = (
        await api.write(
            "post", "/api/schedules", json={"topics": ["算力"], "time": "22:00", "platform": "weixin", "chat_id": "u1"}
        )
    ).json()

    soft = await api.write("delete", f"/api/schedules/{first['id']}")
    assert soft.status_code == 200 and soft.json()["enabled"] is False
    assert len((await api.get("/api/schedules")).json()["items"]) == 2

    hard = await api.write("delete", f"/api/schedules/{second['id']}?purge=true")
    assert hard.status_code == 200 and hard.json() == {"deleted": second["id"]}
    items = (await api.get("/api/schedules")).json()["items"]
    assert [item["id"] for item in items] == [first["id"]]

    assert (await api.write("delete", "/api/schedules/99999")).status_code == 404
