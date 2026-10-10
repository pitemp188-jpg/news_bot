"""
模块: tests.unit.core.test_log
职责: 校验脱敏、trace id 注入与文件日志写入
依赖: core.log
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from newsbot.core import log as log_module


def test_mask_hides_credentials() -> None:
    masked = log_module.mask('Authorization: Bearer abc.def-123 {"api_key": "sk-live-999", "token":"t0k3n"}')
    assert "abc.def-123" not in masked
    assert "sk-live-999" not in masked
    assert "t0k3n" not in masked
    assert masked.count("***") >= 3


def test_trace_id_in_file_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(log_module, "_configured", False)
    handler_owner = logging.getLogger()
    original_handlers = handler_owner.handlers[:]
    log_module.setup_logging(level="INFO", log_dir=tmp_path)
    try:
        log_module.set_trace("trace-42")
        log_module.get_logger("test").info("发生了一件事")
        for handler in handler_owner.handlers:
            handler.flush()
        content = (tmp_path / "newsbot.log").read_text(encoding="utf-8")
        assert "trace-42" in content
        assert "发生了一件事" in content
    finally:
        for handler in handler_owner.handlers[:]:
            if handler not in original_handlers:
                handler_owner.removeHandler(handler)
                handler.close()
        log_module.set_trace("-")


def test_foreign_root_handlers_are_replaced(tmp_path: Path, monkeypatch) -> None:
    """首次配置日志要摘掉第三方挂在根日志上的 handler。

    实测缺陷：`python -m newsbot run` 从未调用 setup_logging，根日志被 browser-use
    的 logging_config 接管（它发现根日志没有 handler 就会清空并装上自己的格式）。
    结果是长跑模式既没有 newsbot.log 落盘、也没有凭证脱敏。
    """
    monkeypatch.setattr(log_module, "_configured", False)
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    foreign = logging.StreamHandler()
    root.addHandler(foreign)
    log_module.setup_logging(level="INFO", log_dir=tmp_path)
    try:
        assert foreign not in root.handlers, "外来 handler 必须被摘掉，否则日志会重复打印"
        assert root.hasHandlers(), "装好 handler 后 hasHandlers() 必须为真"
        # browser-use 的 setup_logging 以此作为"已有配置，别动"的信号，
        # 这条断言正是"我们的文件日志不会被第三方清空"的机制保证。
        assert any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers)
        # 顺带确认它确实写到了指定目录
        log_module.get_logger("test").info("落盘检查")
        for handler in root.handlers:
            handler.flush()
        assert "落盘检查" in (tmp_path / "newsbot.log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
