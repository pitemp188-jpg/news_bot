"""
模块: tests.unit.agent.test_fetch
职责: 校验内网拦截、正文抽取、标题来源、大小上限与错误处理
依赖: agent.tools.fetch
"""

from __future__ import annotations

import socket

import httpx
import pytest
import respx

from newsbot.agent.tools.fetch import FetchTool, extract_text, is_safe_url
from newsbot.core.config import AgentSection
from newsbot.core.errors import RetriableError


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://127.0.0.1:8080/x",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "https://[::1]/",
        "not-a-url",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    assert is_safe_url(url) is False


def test_public_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert is_safe_url("https://example.com/a") is True


def test_dns_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise socket.gaierror("no dns")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert is_safe_url("https://unknown.example") is False


def test_hostname_resolving_to_private_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 0))])
    assert is_safe_url("http://intranet.example/") is False


def test_extract_text_prefers_readable_content() -> None:
    html = "<html><head><title>t</title><style>b{}</style></head><body><p>正文一</p><p>正文二</p></body></html>"
    text = extract_text(html)
    assert "正文一" in text and "正文二" in text
    assert "b{}" not in text


async def test_fetch_local_page(local_server: str) -> None:
    tool = FetchTool(AgentSection(), allow_private=True)
    result = await tool.run(f"{local_server}/article.html")
    assert "推理成本" in result.text
    assert "console.log" not in result.text
    assert result.sources[0].title.startswith("示例资讯")
    assert result.sources[0].url.endswith("/article.html")
    await tool.aclose()


async def test_private_url_blocked_by_default(local_server: str) -> None:
    tool = FetchTool(AgentSection())
    result = await tool.run(f"{local_server}/article.html")
    assert "不允许访问" in result.text
    assert result.sources == []
    await tool.aclose()


async def test_missing_url_is_reported() -> None:
    tool = FetchTool(AgentSection())
    assert "缺少网址" in (await tool.run("")).text
    await tool.aclose()


async def test_download_respects_size_cap(local_server: str) -> None:
    tool = FetchTool(AgentSection(fetch_max_bytes=80), allow_private=True)
    raw = await tool._download(f"{local_server}/article.html")
    assert 0 < len(raw.encode("utf-8")) <= 80
    await tool.aclose()


@respx.mock
async def test_http_error_is_reported_not_raised() -> None:
    respx.get("https://news.example/gone").mock(return_value=httpx.Response(404))
    tool = FetchTool(AgentSection(), allow_private=True)
    result = await tool.run("https://news.example/gone")
    assert "404" in result.text
    await tool.aclose()


@respx.mock
async def test_timeout_is_retriable() -> None:
    respx.get("https://news.example/slow").mock(side_effect=httpx.ConnectTimeout("慢"))
    tool = FetchTool(AgentSection(), allow_private=True)
    with pytest.raises(RetriableError):
        await tool.run("https://news.example/slow")
    await tool.aclose()


@respx.mock
async def test_page_without_body_text_reports_failure() -> None:
    respx.get("https://news.example/empty").mock(
        return_value=httpx.Response(200, text="<html><head></head><body><script>var a=1;</script></body></html>")
    )
    tool = FetchTool(AgentSection(), allow_private=True)
    result = await tool.run("https://news.example/empty")
    assert "未能从页面提取到正文" in result.text
    await tool.aclose()
