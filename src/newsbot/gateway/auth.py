"""
模块: gateway.auth
职责: 入站鉴权——白名单、配对码与待批准队列
依赖: core.log
来源: 思路参考 hermes-agent@908e4a4 gateway/pairing.py 与 platforms/access_policy_mixin.py（MIT）
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

from newsbot.core.log import get_logger

logger = get_logger(__name__)

CODE_TTL = 600.0
ALLOW_ALL = "*"
PAIRING_FILE = "pairing.json"


@dataclass
class PendingRequest:
    """等待主人批准的配对请求。"""

    platform: str
    user_id: str
    code: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > CODE_TTL


def parse_allowed(raw: str) -> set[str]:
    """解析逗号分隔的白名单；空串表示不放行任何人。"""
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


class Authorizer:
    """
    白名单 + 配对码。

    - 配置里的白名单直接放行，`*` 表示全部放行；
    - 未授权用户会拿到一个配对码，主人执行 `approve` 后写入本地白名单；
    - 未授权的入站消息只记录日志，不做任何回复。
    """

    def __init__(self, allowed: dict[str, set[str]] | None = None, *, root: Path | None = None) -> None:
        self._allowed: dict[str, set[str]] = {key: set(value) for key, value in (allowed or {}).items()}
        self._pending: dict[tuple[str, str], PendingRequest] = {}
        self._root = Path(root) if root is not None else None
        self._load()

    # ── 持久化 ──
    @property
    def _path(self) -> Path | None:
        return None if self._root is None else self._root / PAIRING_FILE

    def _load(self) -> None:
        path = self._path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.warning("读取白名单失败: %s", exc)
            return
        stored = data.get("allowed")
        if isinstance(stored, dict):
            for platform, users in stored.items():
                if isinstance(users, list):
                    self._allowed.setdefault(str(platform), set()).update(str(user) for user in users)

    def _save(self) -> None:
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"allowed": {key: sorted(value) for key, value in self._allowed.items()}}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        with suppress(OSError):
            os.chmod(path, 0o600)

    # ── 判定与配对 ──
    def is_allowed(self, platform: str, user_id: str) -> bool:
        users = self._allowed.get(platform, set())
        return ALLOW_ALL in users or str(user_id) in users

    def add(self, platform: str, user_id: str) -> None:
        """直接放行（来自配置审批或手工加入）。"""
        self._allowed.setdefault(platform, set()).add(str(user_id))
        self._pending.pop((platform, str(user_id)), None)
        self._save()

    def request(self, platform: str, user_id: str) -> PendingRequest:
        """为未授权用户生成配对码；重复请求返回同一个未过期码。"""
        key = (platform, str(user_id))
        existing = self._pending.get(key)
        if existing is not None and not existing.is_expired():
            return existing
        request = PendingRequest(platform=platform, user_id=str(user_id), code=f"{secrets.randbelow(1_000_000):06d}")
        self._pending[key] = request
        logger.warning("未授权用户请求配对 %s/%s，配对码 %s", platform, user_id, request.code)
        return request

    def pending(self, *, purge_expired: bool = True) -> list[PendingRequest]:
        if purge_expired:
            self._pending = {key: value for key, value in self._pending.items() if not value.is_expired()}
        return list(self._pending.values())

    def approve(self, platform: str, user_id: str) -> bool:
        """批准配对：写入白名单。"""
        key = (platform, str(user_id))
        if key not in self._pending and not self.is_allowed(platform, user_id):
            return False
        self.add(platform, user_id)
        logger.info("已批准 %s/%s", platform, user_id)
        return True

    def deny(self, platform: str, user_id: str) -> bool:
        removed = self._pending.pop((platform, str(user_id)), None)
        return removed is not None

    def snapshot(self) -> dict[str, list[str]]:
        return {key: sorted(value) for key, value in self._allowed.items()}


def pending_to_dict(request: PendingRequest) -> dict[str, object]:
    """供管理界面展示。"""
    return asdict(request)
