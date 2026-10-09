"""
模块: core.log
职责: 日志初始化——按天轮转、trace id 注入、密钥脱敏
依赖: 无
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_trace: ContextVar[str] = ContextVar("trace_id", default="-")
_configured = False

_MASK_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
)


def mask(text: str) -> str:
    """遮蔽日志中的凭证片段。"""
    for pattern in _MASK_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


def set_trace(trace_id: str) -> None:
    _trace.set(trace_id)


def current_trace() -> str:
    return _trace.get()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace = _trace.get()
        return True


class _MaskFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def setup_logging(*, level: str = "INFO", log_dir: Path | None = None) -> None:
    """配置根日志；重复调用只生效一次。"""
    global _configured
    if _configured:
        return

    formatter = _MaskFormatter("%(asctime)s %(levelname)s [%(trace)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level.upper())

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_TraceFilter())
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_dir / "newsbot.log", when="midnight", backupCount=14, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_TraceFilter())
        root.addHandler(file_handler)

    _configured = True
