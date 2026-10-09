"""
模块: gateway.router
职责: 入站路由与出站投递——鉴权、适配器注册、分段发送与重试、waiting_user 补投递与备用通道
依赖: gateway.auth, gateway.base, core.config, core.db, core.log, core.models
来源: 投递重试思路参考 hermes-agent@908e4a4 gateway/delivery.py（MIT），本项目重写
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select

from newsbot.core.config import DeliverySection
from newsbot.core.db import Database
from newsbot.core.log import get_logger
from newsbot.core.models import Delivery, Report
from newsbot.gateway.auth import Authorizer
from newsbot.gateway.base import BaseAdapter, MessageEvent, SendResult, safe_id

logger = get_logger(__name__)

InboundHandler = Callable[[MessageEvent], Awaitable[None]]


class Router:
    """适配器注册表 + 鉴权入口 + 出站投递；dispatcher 通过 send 调用。"""

    def __init__(
        self,
        db: Database,
        authorizer: Authorizer,
        settings: DeliverySection,
        *,
        inbound: InboundHandler | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db = db
        self._auth = authorizer
        self._settings = settings
        self._inbound = inbound
        self._sleep = sleeper
        self._adapters: dict[str, BaseAdapter] = {}
        self._flushing: set[tuple[str, str]] = set()

    @property
    def platforms(self) -> list[str]:
        return list(self._adapters)

    def set_inbound(self, handler: InboundHandler) -> None:
        self._inbound = handler

    def get(self, platform: str) -> BaseAdapter | None:
        return self._adapters.get(platform)

    def register(self, adapter: BaseAdapter) -> None:
        """注册适配器并把入站事件接到本路由。"""
        self._adapters[adapter.platform] = adapter
        adapter.set_handler(self.handle)

    def online(self) -> dict[str, bool]:
        return {platform: adapter.is_connected for platform, adapter in self._adapters.items()}

    async def start(self) -> dict[str, bool]:
        """启动全部适配器；单个失败不影响其他平台。"""
        for platform, adapter in self._adapters.items():
            try:
                ok = await adapter.connect()
            except Exception as exc:
                logger.error("[router] %s 启动失败: %s", platform, exc)
                ok = False
            if not ok:
                logger.warning("[router] 平台 %s 未就绪", platform)
        return self.online()

    async def stop(self) -> None:
        for platform, adapter in self._adapters.items():
            try:
                await adapter.aclose()
            except Exception as exc:
                logger.warning("[router] %s 关闭异常: %s", platform, exc)

    # ── 入站 ──
    async def handle(self, event: MessageEvent) -> None:
        if not self._auth.is_allowed(event.platform, event.user_id):
            request = self._auth.request(event.platform, event.user_id)
            logger.warning(
                "[router] 忽略未授权消息 %s/%s（配对码 %s，待主人用 newsbot pair approve 批准）",
                event.platform,
                safe_id(event.user_id),
                request.code,
            )
            return
        if self._inbound is None:
            logger.warning("[router] 未注册入站处理器，丢弃来自 %s 的消息", event.platform)
            return
        # 用户重新出现时先补投递此前积压的推送
        await self.flush_waiting(event.platform, event.chat_id)
        await self._inbound(event)

    # ── 出站 ──
    async def send(self, *, platform: str, chat_id: str, text: str, report_id: int | None = None) -> bool:
        """按主通道、备用通道顺序投递，并记录 delivery 状态。"""
        targets = [platform]
        fallback = self._settings.fallback_platform
        if fallback and fallback != platform and fallback in self._adapters:
            targets.append(fallback)

        delivery_id = await self._open_delivery(platform, chat_id, report_id, text)
        last_error = ""
        need_user = False
        attempts = 0
        for target in targets:
            adapter = self._adapters.get(target)
            if adapter is None:
                last_error = f"平台 {target} 未注册"
                continue
            for attempt in range(1, self._settings.max_attempts + 1):
                attempts += 1
                result = await self._send_once(adapter, chat_id, text)
                if result.ok:
                    await self._close_delivery(delivery_id, "sent", attempts, None)
                    logger.info("[router] 已投递到 %s chat=%s", target, safe_id(chat_id))
                    return True
                last_error = result.error or "未知错误"
                if result.need_user:
                    need_user = True
                    break
                if not result.retryable:
                    break
                await self._sleep(min(2.0 * attempt, 6.0))

        status = "waiting_user" if need_user else "failed"
        await self._close_delivery(delivery_id, status, attempts, last_error)
        logger.warning("[router] 投递失败（%s）chat=%s: %s", status, safe_id(chat_id), last_error)
        return False

    async def _send_once(self, adapter: BaseAdapter, chat_id: str, text: str) -> SendResult:
        try:
            return await adapter.send(chat_id, text)
        except Exception as exc:  # 适配器异常按可重试处理
            logger.warning("[router] %s 发送异常: %s", adapter.platform, exc)
            return SendResult.failure(f"{type(exc).__name__}: {exc}", retryable=True)

    async def flush_waiting(self, platform: str, chat_id: str) -> int:
        """把等待补投的推送发给刚出现的目标；返回成功条数。"""
        key = (platform, chat_id)
        if key in self._flushing:
            return 0
        self._flushing.add(key)
        try:
            async with self._db.session() as session:
                rows = (
                    await session.execute(
                        select(Delivery, Report.content)
                        .outerjoin(Report, Report.id == Delivery.report_id)
                        .where(
                            Delivery.platform == platform,
                            Delivery.chat_id == chat_id,
                            Delivery.status == "waiting_user",
                        )
                        .order_by(Delivery.id)
                    )
                ).all()
            sent = 0
            for delivery, report_content in rows:
                adapter = self._adapters.get(delivery.platform)
                if adapter is None:
                    break
                content = delivery.content or report_content or ""
                result = await self._send_once(adapter, chat_id, content)
                if result.ok:
                    await self._close_delivery(delivery.id, "sent", delivery.attempts + 1, None)
                    sent += 1
                    if self._settings.chunk_delay_seconds > 0:
                        await self._sleep(self._settings.chunk_delay_seconds)
                elif result.need_user:
                    await self._close_delivery(delivery.id, "waiting_user", delivery.attempts + 1, result.error)
                    break
                else:
                    await self._close_delivery(delivery.id, "failed", delivery.attempts + 1, result.error)
            if sent:
                logger.info("[router] 补投递 %d 条到 %s/%s", sent, platform, safe_id(chat_id))
            return sent
        finally:
            self._flushing.discard(key)

    # ── delivery 记录 ──
    async def _open_delivery(self, platform: str, chat_id: str, report_id: int | None, content: str) -> int:
        async with self._db.session() as session:
            row = Delivery(platform=platform, chat_id=chat_id, report_id=report_id, content=content)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def _close_delivery(self, delivery_id: int, status: str, attempts: int, error: str | None) -> None:
        async with self._db.session() as session:
            row = await session.get(Delivery, delivery_id)
            if row is None:
                return
            row.status = status
            row.attempts = attempts
            row.error = error
            if status == "sent":
                row.sent_at = datetime.now(UTC)
            await session.commit()
