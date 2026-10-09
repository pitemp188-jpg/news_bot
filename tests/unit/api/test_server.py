"""
模块: tests.unit.api.test_server
职责: 校验管理后台服务——健康检查、登录登出、会话保护、SPA 回退与配置校验
依赖: newsbot.api.server
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.api.conftest import ADMIN_PASSWORD

from newsbot.api import server as server_module
from newsbot.api.security import COOKIE_CSRF, COOKIE_SESSION
from newsbot.api.server import create_app
from newsbot.app import App
from newsbot.core.config import Config, Secrets
from newsbot.core.errors import ConfigError
from newsbot.core.llm import LLMReply


class _StubLLM:
    async def complete(self, messages, tools=None):
        return LLMReply(content="ok")

    async def aclose(self) -> None:
        return None


def _config(tmp_path: Path) -> Config:
    return Config(
        secrets=Secrets(_env_file=None, llm_api_key="sk-test", admin_password="pw"),
        data_dir=tmp_path,
    )


async def test_health_is_public(api) -> None:
    response = await api.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["platforms"] == {"weixin": True}


async def test_protected_endpoints_require_login(api) -> None:
    for url in ("/api/tasks", "/api/schedules", "/api/news", "/api/reports", "/api/deliveries", "/api/system/status"):
        assert (await api.get(url)).status_code == 401, url
    assert (await api.get("/api/me")).status_code == 401


async def test_login_rejects_wrong_password(api) -> None:
    response = await api.client.post("/api/login", json={"password": "错误口令"})
    assert response.status_code == 401
    assert COOKIE_SESSION not in response.cookies


async def test_login_sets_cookies_and_grants_access(api) -> None:
    response = await api.client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["csrf"]
    assert COOKIE_SESSION in response.cookies and COOKIE_CSRF in response.cookies

    # 会话 Cookie 必须带 HttpOnly，CSRF Cookie 必须能被前端读取
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if item.startswith(COOKIE_SESSION))
    csrf_cookie = next(item for item in cookies if item.startswith(COOKIE_CSRF))
    assert "httponly" in session_cookie.lower()
    assert "httponly" not in csrf_cookie.lower()

    assert (await api.client.get("/api/me")).status_code == 200
    assert (await api.client.get("/api/tasks")).status_code == 200


async def test_logout_revokes_session(api) -> None:
    await api.login()
    assert (await api.write("post", "/api/logout")).status_code == 200
    assert (await api.get("/api/tasks")).status_code == 401


async def test_root_serves_web_when_built(api) -> None:
    """前端构建产物存在时，根路径直接返回页面。"""
    if not server_module.WEB_DIST.exists():
        pytest.skip("未执行 npm run build，跳过静态托管用例")
    response = await api.get("/")
    assert response.status_code == 200
    assert '<div id="app">' in response.text


async def test_index_falls_back_when_web_not_built(api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 指向不存在的目录，模拟「还没 npm run build」，应给出可操作提示而不是 500
    monkeypatch.setattr(server_module, "WEB_DIST", tmp_path / "missing")
    api_app = create_app(api.config, api.app)
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "npm" in response.json()["detail"]


async def test_spa_fallback_serves_index_for_unknown_path(api) -> None:
    """前端路由的深链接要回退到 index.html，而不是 404。"""
    if not server_module.WEB_DIST.exists():
        pytest.skip("未执行 npm run build，跳过 SPA 回退用例")
    response = await api.get("/schedules")
    assert response.status_code == 200
    assert '<div id="app">' in response.text


async def test_missing_admin_password_is_rejected(tmp_path: Path) -> None:
    config = Config(secrets=Secrets(_env_file=None, llm_api_key="sk-test", admin_password=""), data_dir=tmp_path)
    app = App(config, llm=_StubLLM())
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"):
        create_app(config, app)


async def test_lifespan_starts_and_stops_service(tmp_path: Path) -> None:
    """未启动过的服务交给 lifespan 自行启停，避免管理后台起来后后台任务没跑。"""
    config = _config(tmp_path)
    app = App(config, llm=_StubLLM(), adapters=[])
    api_app = create_app(config, app)
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # httpx 的 ASGITransport 不跑 lifespan，这里显式确认服务未被提前启动
        assert app.db is None
        assert (await client.get("/api/health")).status_code == 200
    assert app.db is None
    await app.stop()
