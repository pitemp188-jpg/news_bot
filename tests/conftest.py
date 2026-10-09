"""
模块: tests.conftest
职责: pytest 全局夹具——拦截对外网络、提供临时数据目录
依赖: 无
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

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
