"""
模块: scripts.hooks.commit_msg
职责: 校验提交信息格式——<type>(<scope>): <摘要>，fix 类必须写明问题原因 / 修复方式 / 注意事项
依赖: 无（仅标准库）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TYPES = ("feat", "fix", "refactor", "test", "docs", "chore", "perf", "wip")
SUBJECT_RE = re.compile(rf"^({'|'.join(TYPES)})\([a-z0-9_/-]+\): \S")
SKIP_PREFIXES = ("Merge", "Revert", "fixup!", "squash!")
FIX_KEYS = ("问题原因:", "修复方式:", "注意事项:")


def validate(message: str) -> list[str]:
    """返回错误列表；空列表表示通过。"""
    subject = message.strip().splitlines()[0] if message.strip() else ""
    if not subject:
        return ["提交信息为空"]
    if subject.startswith(SKIP_PREFIXES):
        return []
    errors: list[str] = []
    if not SUBJECT_RE.match(subject):
        errors.append(f"标题应为 <type>(<scope>): <摘要>，type 取 {'|'.join(TYPES)}；当前: {subject}")
    if subject.startswith("fix(") or subject.startswith("fix:"):
        errors.extend(f"fix 提交缺少 {key}" for key in FIX_KEYS if key not in message)
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: commit_msg.py <commit-message-file>", file=sys.stderr)
        return 2
    message = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    errors = validate(message)
    for error in errors:
        print(f"[commit-msg] {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
