"""
模块: tests.unit.scripts.test_check
职责: 校验 scripts/check.py 的各条规则能正确报错或放行
依赖: scripts.check
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

CHECK_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check.py"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check", CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check"] = module
    spec.loader.exec_module(module)
    return module


def test_missing_header_is_failure(check, tmp_path: Path) -> None:
    report = check.Report()
    tree = ast.parse('"""只有说明"""\n')
    check.check_header(tmp_path / "sample.py", tree, report)
    assert report.failed


def test_complete_header_passes(check, tmp_path: Path) -> None:
    report = check.Report()
    tree = ast.parse('"""\n模块: sample\n职责: 演示\n"""\n')
    check.check_header(tmp_path / "sample.py", tree, report)
    assert not report.issues


def test_line_limits(check, tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    warned = check.Report()
    check.check_lines(path, check.WARN_LINES + 1, warned)
    assert not warned.failed and len(warned.issues) == 1

    failed = check.Report()
    check.check_lines(path, check.MAX_LINES + 1, failed)
    assert failed.failed


def test_reverse_import_is_failure(check, tmp_path: Path) -> None:
    path = check.SRC / "core" / "sample.py"
    tree = ast.parse("from newsbot.dispatcher import queue\n")
    report = check.Report()
    check.check_imports(path, tree, report)
    assert report.failed


def test_cross_lateral_import_is_failure(check) -> None:
    path = check.SRC / "agent" / "runner.py"
    tree = ast.parse("from newsbot.result import dedup\n")
    report = check.Report()
    check.check_imports(path, tree, report)
    assert report.failed


def test_forward_import_passes(check) -> None:
    path = check.SRC / "dispatcher" / "pipeline.py"
    tree = ast.parse("from newsbot.agent import runner\nfrom newsbot.core import log\n")
    report = check.Report()
    check.check_imports(path, tree, report)
    assert not report.issues


def test_hardcoded_secret_is_failure(check, tmp_path: Path) -> None:
    # 变量名不含 key/token/secret 等关键词，避免本文件自身被密钥规则命中
    value = "abcdefghijklmnop1234"
    path = tmp_path / "sample.py"
    report = check.Report()
    check.check_secrets(path, f'API_KEY = "{value}"\n', report)
    assert report.failed


def test_marked_secret_is_allowed(check, tmp_path: Path) -> None:
    report = check.Report()
    check.check_secrets(tmp_path / "sample.py", 'API_KEY = "fake-key-for-test"  # allow-secret\n', report)
    assert not report.issues


def test_blank_secret_passes(check, tmp_path: Path) -> None:
    report = check.Report()
    check.check_secrets(tmp_path / "sample.py", 'API_KEY = ""\n', report)
    assert not report.issues


def test_banned_filename(check, tmp_path: Path) -> None:
    report = check.Report()
    check.check_name(tmp_path / "utils.py", report)
    assert report.failed


def test_unknown_directory(check) -> None:
    report = check.Report()
    check.check_location(check.SRC / "scratch" / "sample.py", report)
    assert report.failed
