"""
模块: agent.tools.fetch
职责: 抓取工具——正文抽取、大小上限、超时、内网地址拦截
依赖: agent.tools.base, core.config, core.errors, core.log
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx

from newsbot.agent.tools.base import Source, ToolResult
from newsbot.core.config import AgentSection
from newsbot.core.errors import RetriableError
from newsbot.core.log import get_logger

logger = get_logger(__name__)

MAX_TEXT_CHARS = 8000
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
    """兜底正文抽取：去掉脚本样式，拼接可见文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def _strip_tags(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def extract_text(html: str) -> str:
    """优先用 trafilatura 抽正文，失败则退回标签剥离。"""
    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        if extracted and extracted.strip():
            return extracted.strip()
    except ImportError:  # pragma: no cover - 依赖缺失时走兜底
        logger.debug("未安装 trafilatura，使用标签剥离兜底")
    return _strip_tags(html)


def is_safe_url(url: str) -> bool:
    """只允许 http(s)，且目标不能是内网、回环或链路本地地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, None)]
        except (socket.gaierror, OSError):
            return False
    return all(not (_is_private(address)) for address in addresses)


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


class FetchTool:
    """抓取单页并抽取正文；返回内容截断到可读长度。"""

    name = "fetch"
    description = "抓取指定网址并抽取正文。搜索结果摘要不够时使用，一次只抓一个页面。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "要抓取的完整网址"}},
        "required": ["url"],
    }

    def __init__(
        self,
        settings: AgentSection,
        *,
        client: httpx.AsyncClient | None = None,
        allow_private: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self._settings = settings
        self._allow_private = allow_private
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def run(self, url: str, **_: Any) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult.failure("缺少网址")
        if not (self._allow_private or is_safe_url(url)):
            logger.warning("拒绝抓取不安全地址: %s", url)
            return ToolResult.failure(f"地址不允许访问（内网或非 http/https）: {url}")

        try:
            html = await self._download(url)
        except httpx.TimeoutException as exc:
            raise RetriableError(f"抓取超时: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(f"目标返回 {exc.response.status_code}")

        text = extract_text(html)
        if not text:
            return ToolResult.failure(f"未能从页面提取到正文: {url}")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n…（内容过长已截断）"
        title = _title_of(html) or url
        return ToolResult(text=text, sources=[Source(title=title, url=url)])

    async def _download(self, url: str) -> str:
        limit = self._settings.fetch_max_bytes
        chunks: list[bytes] = []
        total = 0
        async with self._http().stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= limit:
                    logger.warning("页面超过上限 %d 字节，已截断: %s", limit, url)
                    break
        # 单个分片可能一次就超过上限，这里再按字节硬截一次
        return b"".join(chunks)[:limit].decode("utf-8", errors="replace")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def _title_of(html: str) -> str:
    parser = _TitleParser()
    try:
        parser.feed(html)
    except Exception:  # pragma: no cover - 畸形 HTML
        return ""
    return parser.title
