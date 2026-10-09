"""
模块: tests.unit.api.test_deps
职责: 校验依赖注入——服务未就绪时的 503、鉴权依赖的拒绝与放行
依赖: newsbot.api.deps
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from newsbot.api.deps import Services, get_services, require_auth
from newsbot.app import App
from newsbot.core.config import Config, Secrets


def _request(app: FastAPI, *, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Request:
    """构造一个最小 Request，只带我们需要读的 cookie 与 header。"""
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    raw_headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in (cookies or {}).items()).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw_headers,
        "app": app,
    }
    return Request(scope)


def _config(tmp_path) -> Config:
    return Config(secrets=Secrets(_env_file=None, llm_api_key="sk-test", admin_password="pw"), data_dir=tmp_path)


def test_get_services_raises_when_not_ready(tmp_path) -> None:
    app = FastAPI()
    request = _request(app)
    try:
        get_services(request)
    except HTTPException as exc:
        assert exc.status_code == 503
    else:  # pragma: no cover - 明确失败比静默通过好
        raise AssertionError("未装配服务时应返回 503")


def test_services_db_raises_before_start(tmp_path) -> None:
    config = _config(tmp_path)
    services = Services(config=config, app=App(config))
    try:
        _ = services.db
    except HTTPException as exc:
        assert exc.status_code == 503
    else:  # pragma: no cover
        raise AssertionError("服务未启动时取数据库应返回 503")


def test_get_services_returns_attached_instance(tmp_path) -> None:
    app = FastAPI()
    config = _config(tmp_path)
    services = Services(config=config, app=App(config))
    app.state.services = services
    assert get_services(_request(app)) is services


def test_require_auth_rejects_without_session(tmp_path) -> None:
    app = FastAPI()
    app.state.sessions = _FakeSessions(valid=False)
    try:
        require_auth(_request(app))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:  # pragma: no cover
        raise AssertionError("无会话时应返回 401")


def test_require_auth_accepts_valid_session(tmp_path) -> None:
    app = FastAPI()
    app.state.sessions = _FakeSessions(valid=True)
    require_auth(_request(app, cookies={"nb_session": "t", "nb_csrf": "c"}, headers={"x-csrf-token": "c"}))


def test_require_auth_enforces_csrf(tmp_path) -> None:
    app = FastAPI()
    app.state.sessions = _FakeSessions(valid=True)
    try:
        require_auth(_request(app, cookies={"nb_session": "t"}))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:  # pragma: no cover
        raise AssertionError("写操作缺少 CSRF 令牌时应返回 403")


class _FakeSessions:
    def __init__(self, *, valid: bool) -> None:
        self._valid = valid

    def valid(self, _token: str | None) -> bool:
        return self._valid
