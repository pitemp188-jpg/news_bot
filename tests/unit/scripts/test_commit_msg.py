"""
模块: tests.unit.scripts.test_commit_msg
职责: 校验提交信息规则的各类通过 / 拒绝场景
依赖: scripts.hooks.commit_msg
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[3] / "scripts" / "hooks" / "commit_msg.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("commit_msg", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_msg"] = module
    spec.loader.exec_module(module)
    return module


def test_valid_feat_passes(hook) -> None:
    assert hook.validate("feat(gateway): 新增 QQ 适配器\n\n任务: T2.3\n") == []


def test_merge_commit_skipped(hook) -> None:
    assert hook.validate("Merge pull request #1 from feat/m0-bootstrap\n") == []


def test_empty_message_rejected(hook) -> None:
    assert hook.validate("   \n")


def test_wrong_format_rejected(hook) -> None:
    assert hook.validate("改了一些东西\n")


def test_unknown_type_rejected(hook) -> None:
    assert hook.validate("feature(core): 新增模块\n")


def test_fix_requires_three_sections(hook) -> None:
    errors = hook.validate("fix(gateway): 微信长消息被截断\n")
    assert len(errors) == 3


def test_fix_with_sections_passes(hook) -> None:
    message = (
        "fix(gateway): 微信长消息被截断\n\n"
        "问题原因: 分段未按平台计数规则\n"
        "修复方式: 改按 UTF-16 单元计数\n"
        "注意事项: 旧投递记录不受影响\n"
    )
    assert hook.validate(message) == []
