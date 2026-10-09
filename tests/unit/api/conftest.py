"""
模块: tests.unit.api.conftest
职责: 管理后台测试夹具——组装真实 App + FastAPI，并提供带会话的 HTTP 客户端
依赖: newsbot.api.server, newsbot.app, tests.fakes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.fakes.adapter import FakeAdapter
from tests.fakes.llm import FakeLLM, reply

from newsbot.app import App
from newsbot.core.config import Config, ScheduleSection, Secrets

ADMIN_PASSWORD = "pw-for-test"


class ApiHarness:
    """把服务、客户端与常用断言动作打包，减少每个用例的样板。"""

    def __init__(self, config: Config, app: App, adapter: FakeAdapter, client: AsyncClient) -> None:
        self.config = config
        self.app = app
        self.adapter = adapter
        self.client = client
        self.csrf = ""

    async def login(self) -> None:
        """登录并记住 CSRF 令牌，写入客户端请求头。"""
        response = await self.client.post("/api/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf"]
        self.client.headers["x-csrf-token"] = self.csrf

    async def get(self, url: str) -> Any:
        return await self.client.get(url)

    async def write(self, method: str, url: str, **kwargs: Any) -> Any:
        """写操作：默认带上 CSRF 令牌。"""
        headers = kwargs.pop("headers", {})
        headers.setdefault("x-csrf-token", self.csrf)
        return await self.client.request(method, url, headers=headers, **kwargs)


@pytest.fixture
async def api(tmp_path: Path):
    """真实组装的服务 + 真实 FastAPI 应用；只把平台与模型换成替身。"""
    pytest.importorskip("fastapi")
    from newsbot.api.server import create_app

    config = Config(
        secrets=Secrets(
            _env_file=None, llm_api_key="sk-test", admin_password=ADMIN_PASSWORD, weixin_allowed_users="u1"
        ),
        schedule=ScheduleSection(default_time="21:00", default_topics=["AI"]),
        data_dir=tmp_path,
    )
    adapter = FakeAdapter(platform="weixin", max_length=2000)
    app = App(config, llm=FakeLLM(replies=[reply("结论 [1]")]), adapters=[adapter])
    await app.start()

    api_app = create_app(config, app)
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield ApiHarness(config, app, adapter, client)

    await app.stop()
    if app.db is not None:
        await app.db.dispose()
