"""
模块: tests.unit.api.test_security
职责: 校验登录态与 CSRF——会话过期、常量时间口令比较、双提交令牌
依赖: newsbot.api.security
"""

from __future__ import annotations

import pytest

from newsbot.api.security import HEADER_CSRF, Sessions, check_password


def test_sessions_issue_and_validate() -> None:
    sessions = Sessions()
    token = sessions.issue()
    assert token and sessions.valid(token)
    assert not sessions.valid("不存在")
    assert not sessions.valid(None)
    assert len(sessions) == 1


def test_sessions_expire_and_revoke() -> None:
    # 用可注入时钟推进，不依赖真实 sleep
    now = [1000.0]
    sessions = Sessions(ttl=60.0, clock=lambda: now[0])
    token = sessions.issue()
    assert sessions.valid(token)

    now[0] += 59.0
    assert sessions.valid(token), "未到期应继续有效"
    now[0] += 2.0
    assert not sessions.valid(token), "过期后必须失效"
    assert len(sessions) == 0

    fresh = sessions.issue()
    sessions.revoke(fresh)
    assert not sessions.valid(fresh)
    sessions.revoke(None)  # 不应抛错


def test_check_password_is_exact() -> None:
    assert check_password("s3cret", "s3cret")
    assert not check_password("s3cret", "s3cre")
    assert not check_password("s3cret", "")
    assert not check_password("", "anything"), "未配置口令时必须拒绝一切登录"


@pytest.mark.parametrize(
    ("method", "url"),
    [("post", "/api/tasks"), ("patch", "/api/schedules/1"), ("delete", "/api/schedules/1"), ("post", "/api/logout")],
)
async def test_csrf_required_for_write(api, method: str, url: str) -> None:
    await api.login()
    api.client.headers.pop(HEADER_CSRF, None)
    response = await api.client.request(method, url, json={})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


async def test_csrf_mismatch_is_rejected(api) -> None:
    await api.login()
    response = await api.client.post("/api/tasks", json={"query": "x"}, headers={HEADER_CSRF: "wrong-token"})
    assert response.status_code == 403


async def test_safe_methods_need_no_csrf(api) -> None:
    await api.login()
    assert (await api.client.get("/api/tasks")).status_code == 200
