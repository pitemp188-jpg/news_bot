"""
模块: tests.conftest
职责: pytest 全局夹具——拦截对外网络、提供临时数据目录
依赖: 无
"""

from __future__ import annotations

import functools
import http.server
import socket
import threading
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pages"

# 本地测试服务（tests/fixtures/pages）允许访问，其余外部地址一律拒绝
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guard(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, tuple) and address else address
        if str(host) not in _LOOPBACK:
            raise RuntimeError(f"测试禁止访问外部网络: {host}；请使用 tests/fakes 中的替身或 respx mock")
        return real_connect(self, address, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把运行时数据目录指向临时目录，避免污染 data/。"""
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setenv("NEWSBOT_DATA_DIR", str(target))
    return target


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def local_server() -> str:
    """在回环地址上提供 tests/fixtures/pages，供抓取与端到端测试使用。"""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
