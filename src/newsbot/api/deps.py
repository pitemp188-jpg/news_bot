"""
模块: api.deps
职责: 依赖注入——把运行时组件挂到 request.app.state，并提供鉴权依赖
依赖: api.security, app, core.config
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from newsbot.api.security import COOKIE_SESSION, require_csrf
from newsbot.app import App
from newsbot.core.config import Config
from newsbot.core.db import Database


@dataclass
class Services:
    """路由需要的运行时组件；由 api.server 在启动时装配。"""

    config: Config
    app: App

    @property
    def db(self) -> Database:
        """服务真正启动后才有数据库；未启动时明确报 503 而不是抛 AttributeError。"""
        if self.app.db is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务尚未启动")
        return self.app.db


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务未就绪")
    return services


def require_auth(request: Request) -> None:
    """除登录与健康检查外的所有接口都要带有效会话；写操作额外校验 CSRF。"""
    sessions = getattr(request.app.state, "sessions", None)
    if sessions is None or not sessions.valid(request.cookies.get(COOKIE_SESSION)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或登录已过期")
    require_csrf(request)


Authed = Depends(require_auth)
