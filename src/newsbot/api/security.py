"""
模块: api.security
职责: 管理后台登录态与 CSRF——口令换 HttpOnly Cookie、双提交令牌校验
依赖: core.config, core.errors
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

COOKIE_SESSION = "nb_session"
COOKIE_CSRF = "nb_csrf"
HEADER_CSRF = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_TTL = 12 * 3600.0


@dataclass
class Sessions:
    """进程内登录态；重启即失效，符合单用户管理后台的定位。"""

    ttl: float = DEFAULT_TTL
    clock: Callable[[], float] = time.monotonic
    _issued: dict[str, float] = field(default_factory=dict)

    def issue(self) -> str:
        self.purge()
        token = secrets.token_urlsafe(32)
        self._issued[token] = self.clock() + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expiry = self._issued.get(token)
        if expiry is None:
            return False
        if expiry < self.clock():
            self._issued.pop(token, None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._issued.pop(token, None)

    def purge(self) -> None:
        now = self.clock()
        for token in [key for key, expiry in self._issued.items() if expiry < now]:
            self._issued.pop(token, None)

    def __len__(self) -> int:
        return len(self._issued)


def check_password(expected: str, given: str) -> bool:
    """常量时间比较，避免口令被逐字符试探。"""
    if not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), (given or "").encode("utf-8"))


def require_csrf(request: Request) -> None:
    """双提交校验：写操作必须带上与 Cookie 一致的 X-CSRF-Token。"""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(COOKIE_CSRF)
    header = request.headers.get(HEADER_CSRF)
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 校验失败：缺少或错误的 X-CSRF-Token")
