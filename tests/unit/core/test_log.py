"""
模块: tests.unit.core.test_log
职责: 校验脱敏、trace id 注入与文件日志写入
依赖: core.log
"""

from __future__ import annotations

import logging
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
