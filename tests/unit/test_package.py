"""
模块: tests.unit.test_package
职责: 校验包可导入、版本号可读
依赖: newsbot
"""

import newsbot


def test_version_is_set() -> None:
    assert newsbot.__version__
