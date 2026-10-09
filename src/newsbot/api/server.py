"""
模块: api.server
职责: 管理后台服务——登录（口令 + HttpOnly Cookie + CSRF）、健康检查、REST 路由、前端静态托管
依赖: api.deps, api.routes.*, api.security, app, core.config, core.errors
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from newsbot.api.deps import Services
from newsbot.api.routes import news, schedules, system, tasks
from newsbot.api.security import COOKIE_CSRF, COOKIE_SESSION, Sessions, check_password
from newsbot.app import App, build_app
from newsbot.core.config import Config, get_config
from newsbot.core.errors import ConfigError
from newsbot.core.log import get_logger

logger = get_logger(__name__)

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
SESSION_COOKIE_MAX_AGE = 12 * 3600


class LoginPayload(BaseModel):
    """登录口令。"""

    password: str = Field(min_length=1, max_length=256)


def _cookie_secure(config: Config) -> bool:
    """本机回环访问走 http，不能给 Cookie 加 Secure 否则浏览器不发送。"""
    return config.api.host not in {"127.0.0.1", "localhost", "::1"}


def create_app(config: Config, runtime: App | None = None) -> FastAPI:
    """构造管理后台；runtime 传 None 时按配置自行组装完整服务。"""
    if not config.secrets.admin_password:
        raise ConfigError("管理后台缺少 ADMIN_PASSWORD，请在 .env 中设置后再启动 api")

    service = runtime or build_app(config)
    services = Services(config=config, app=service)

    @asynccontextmanager
    async def lifespan(_api: FastAPI) -> AsyncIterator[None]:
        """后台任务随管理后台一起启停。"""
        started = service.db is not None and service.queue is not None
        if not started:
            await service.start()
        logger.info("管理后台已启动")
        try:
            yield
        finally:
            if not started:
                await service.stop()

    api = FastAPI(
        title="newsbot 管理后台",
        version="0.7.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    api.state.services = services
    api.state.sessions = Sessions()
    secure = _cookie_secure(config)

    # ── 健康检查与登录 ──
    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        app = services.app
        return {
            "ok": True,
            "version": api.version,
            "platforms": app.router.online() if app.router is not None else {},
            "queue": {"depth": app.queue.depth if app.queue else 0, "running": app.queue.running if app.queue else 0},
        }

    @api.post("/api/login")
    async def login(payload: LoginPayload, response: Response) -> dict[str, Any]:
        if not check_password(config.secrets.admin_password, payload.password):
            logger.warning("管理后台登录失败")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "口令不正确")
        sessions: Sessions = api.state.sessions
        token = sessions.issue()
        csrf = Sessions().issue()
        response.set_cookie(
            COOKIE_SESSION,
            token,
            max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
        response.set_cookie(
            COOKIE_CSRF, csrf, max_age=SESSION_COOKIE_MAX_AGE, httponly=False, samesite="lax", secure=secure, path="/"
        )
        return {"ok": True, "csrf": csrf}

    @api.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        from newsbot.api.security import require_csrf

        require_csrf(request)
        sessions: Sessions = api.state.sessions
        sessions.revoke(request.cookies.get(COOKIE_SESSION))
        response.delete_cookie(COOKIE_SESSION, path="/")
        response.delete_cookie(COOKIE_CSRF, path="/")
        return {"ok": True}

    @api.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        sessions: Sessions = api.state.sessions
        if not sessions.valid(request.cookies.get(COOKIE_SESSION)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        return {"authenticated": True, "timezone": config.app.timezone}

    # ── 业务路由 ──
    api.include_router(tasks.router, prefix="/api")
    api.include_router(schedules.router, prefix="/api")
    api.include_router(news.router, prefix="/api")
    api.include_router(system.router, prefix="/api")

    # ── 前端托管 ──
    _mount_web(api)
    return api


def _mount_web(api: FastAPI) -> None:
    """web/dist 存在时直接托管；用 SPA 回退保证前端路由可用。"""
    if not WEB_DIST.exists():
        logger.warning("未找到 web/dist，管理界面需先执行 npm run build（接口仍可用）")

        @api.get("/")
        async def _missing_web() -> JSONResponse:
            return JSONResponse(
                {"detail": "前端尚未构建：请在 web/ 下执行 npm install && npm run build"}, status_code=200
            )

        return

    assets = WEB_DIST / "assets"
    if assets.exists():
        api.mount("/assets", StaticFiles(directory=assets), name="assets")

    @api.get("/")
    async def _index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    @api.get("/{path:path}")
    async def _spa(path: str, request: Request) -> Response:
        """静态文件存在就返回文件，否则回退到 index.html 交给前端路由。"""
        if path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "接口不存在")
        candidate = (WEB_DIST / path).resolve()
        if WEB_DIST in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")


async def serve_api(host: str | None = None, port: int | None = None) -> int:
    """CLI 入口：启动管理后台（同时启动机器人的后台任务）。"""
    import uvicorn

    config = get_config()
    from newsbot.core.log import setup_logging

    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    runtime = build_app(config)
    await runtime.start()
    api = create_app(config, runtime)
    server = uvicorn.Server(
        uvicorn.Config(api, host=host or config.api.host, port=port or config.api.port, log_level="warning")
    )
    try:
        await server.serve()
    finally:
        await runtime.stop()
    return 0
