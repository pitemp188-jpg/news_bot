"""
模块: core.errors
职责: 统一错误类型，各层据此决定重试、降级还是直接失败
依赖: 无
"""

from __future__ import annotations


class NewsbotError(Exception):
    """项目内所有自定义异常的基类。"""


class ConfigError(NewsbotError):
    """配置缺失或非法。"""


class AuthError(NewsbotError):
    """密钥、凭证或权限问题，重试无用。"""


class RetriableError(NewsbotError):
    """网络抖动、限流等瞬时错误，可重试。"""


class FatalError(NewsbotError):
    """重试无法解决，任务直接判失败。"""


class TaskTimeoutError(NewsbotError):
    """任务执行超时。"""


class BlockedError(NewsbotError):
    """需要人工介入（扫码登录、申请凭证），暂停重试。"""
