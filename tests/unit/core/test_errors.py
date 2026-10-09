"""
模块: tests.unit.core.test_errors
职责: 校验自定义错误类型的继承关系与可捕获性
依赖: core.errors
"""

from __future__ import annotations

import pytest

from newsbot.core.errors import (
    AuthError,
    BlockedError,
    FatalError,
    NewsbotError,
    RetriableError,
    TaskTimeoutError,
)


@pytest.mark.parametrize(
    "error_type",
    [AuthError, BlockedError, FatalError, RetriableError, TaskTimeoutError],
)
def test_all_inherit_base(error_type: type[NewsbotError]) -> None:
    assert issubclass(error_type, NewsbotError)


def test_base_inherits_exception() -> None:
    assert issubclass(NewsbotError, Exception)
