"""
模块: scripts.check
职责: 项目统一质量检查入口——规则校验（行数 / 模块头 / 目录 / 命名 / 依赖方向 / 密钥）串联 ruff、mypy、pytest
依赖: 无（仅标准库）
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "newsbot"
TESTS = ROOT / "tests"
SCRIPTS = ROOT / "scripts"

WARN_LINES = 700
MAX_LINES = 1400

# 依赖方向：数值越小越底层，只允许从高层 import 低层
LAYERS = {"api": 3, "gateway": 3, "dispatcher": 2, "agent": 1, "result": 1, "core": 0}
# 同层但互不依赖的模块（禁止横向 import）
FORBIDDEN_PAIRS = {("agent", "result"), ("result", "agent")}

ALLOWED_DIRS = {"core", "gateway", "dispatcher", "agent", "agent/tools", "result", "api", "api/routes"}
ALLOWED_ROOT_FILES = {"__init__.py", "__main__.py", "app.py"}
BANNED_NAMES = {"utils.py", "common.py", "helpers.py", "misc.py"}

SECRET_RE = re.compile(
    r"""(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|secret|password|passwd)\b\s*[:=]\s*["']([A-Za-z0-9_\-/+=]{16,})["']"""
)
# 测试替身里的假密钥可加该标记豁免
ALLOW_SECRET_MARKER = "allow-secret"
SENSITIVE_GIT_PATHS = (re.compile(r"^\.env$"), re.compile(r"^data/"), re.compile(r"\.db(-|$)"))


@dataclass
class Issue:
    level: str  # "fail" | "warn"
    path: Path
    line: int
    message: str

    def render(self) -> str:
        try:
            where = f"{self.path.relative_to(ROOT)}:{self.line}"
        except ValueError:
            where = str(self.path)
        return f"[{self.level}] {where}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def fail(self, path: Path, message: str, line: int = 0) -> None:
        self.issues.append(Issue("fail", path, line, message))

    def warn(self, path: Path, message: str, line: int = 0) -> None:
        self.issues.append(Issue("warn", path, line, message))

    @property
    def failed(self) -> bool:
        return any(issue.level == "fail" for issue in self.issues)


def source_files() -> list[Path]:
    """参与规则校验的源码文件：src、tests、scripts。"""
    files = [*SRC.rglob("*.py"), *TESTS.rglob("*.py"), *SCRIPTS.rglob("*.py")]
    return sorted(path for path in files if "__pycache__" not in path.parts)


def _parse(path: Path, report: Report) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        report.fail(path, f"语法错误: {exc.msg}", exc.lineno or 0)
        return None


def check_lines(path: Path, count: int, report: Report) -> None:
    if count > MAX_LINES:
        report.fail(path, f"{count} 行，超过硬上限 {MAX_LINES} 行")
    elif count > WARN_LINES:
        report.warn(path, f"{count} 行，超过 {WARN_LINES} 行，需在 PR 中说明")


def check_header(path: Path, tree: ast.Module, report: Report) -> None:
    docstring = ast.get_docstring(tree) or ""
    missing = [key for key in ("模块:", "职责:") if key not in docstring]
    if missing:
        report.fail(path, f"模块头注释缺少 {'、'.join(missing)}", 1)


def check_location(path: Path, report: Report) -> None:
    if SRC not in path.parents:
        return
    rel = path.relative_to(SRC)
    if len(rel.parts) == 1:
        if rel.name not in ALLOWED_ROOT_FILES:
            report.fail(path, f"根目录只允许放 {'、'.join(sorted(ALLOWED_ROOT_FILES))}")
        return
    parent = rel.parent.as_posix()
    if parent not in ALLOWED_DIRS:
        report.fail(path, f"目录 src/newsbot/{parent} 未在 docs/02-structure.md 中定义")


def check_name(path: Path, report: Report) -> None:
    if path.name in BANNED_NAMES or path.name.startswith("tmp_"):
        report.fail(path, "禁止的杂物文件名，请归入对应模块或 core/")


def check_imports(path: Path, tree: ast.Module, report: Report) -> None:
    own = _file_layer(path)
    if own is None:
        return
    for node in ast.walk(tree):
        for dotted in _imported_modules(node):
            other = _module_layer(dotted)
            if other is None or other == own:
                continue
            if (own, other) in FORBIDDEN_PAIRS:
                report.fail(path, f"{own} 不允许 import {other}", node.lineno)
            elif LAYERS[own] < LAYERS[other]:
                report.fail(path, f"依赖方向错误: {own} → {other}", node.lineno)


def check_secrets(path: Path, text: str, report: Report) -> None:
    if path.name.endswith(".example"):
        return
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_SECRET_MARKER in line:
            continue
        if SECRET_RE.search(line):
            report.fail(path, "疑似硬编码密钥，请改用 core.config.settings", number)


def _file_layer(path: Path) -> str | None:
    if SRC not in path.parents:
        return None
    parts = path.relative_to(SRC).parts
    return parts[0] if len(parts) > 1 else None


def _module_layer(dotted: str) -> str | None:
    parts = dotted.split(".")
    if len(parts) < 2 or parts[0] != "newsbot":
        return None
    return parts[1] if parts[1] in LAYERS else None


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def check_files(report: Report) -> None:
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        check_lines(path, len(text.splitlines()), report)
        check_location(path, report)
        check_name(path, report)
        check_secrets(path, text, report)
        tree = _parse(path, report)
        if tree is None:
            continue
        check_header(path, tree, report)
        check_imports(path, tree, report)


def check_tests_mirror(report: Report) -> None:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.name == "__init__.py":
            continue
        target = TESTS / "unit" / rel.parent / f"test_{rel.name}"
        if not target.exists():
            report.warn(target, f"缺少与 src/newsbot/{rel.as_posix()} 对应的单元测试")


def check_tracked(report: Report) -> None:
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return
    for tracked in result.stdout.splitlines():
        normalized = tracked.replace("\\", "/")
        if any(pattern.search(normalized) for pattern in SENSITIVE_GIT_PATHS):
            report.fail(ROOT / normalized, "密钥或运行时数据不应提交")


def print_report(report: Report) -> None:
    for issue in report.issues:
        print(issue.render())
    fails = sum(issue.level == "fail" for issue in report.issues)
    warns = len(report.issues) - fails
    print(f"\n规则检查: {fails} 个错误, {warns} 个警告")


def run_step(name: str, args: list[str]) -> bool:
    print(f"\n=== {name} ===")
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode == 0


def quality_steps(fast: bool, live: bool) -> list[tuple[str, list[str]]]:
    pytest_args = ["-m", "pytest"]
    if fast:
        pytest_args.append("tests/unit")
    else:
        pytest_args += ["--cov", "--cov-report=term-missing:skip-covered"]
    if live:
        # 追加 live 组：跑除 soak 之外的全部测试
        pytest_args += ["-m", "not soak"]
    steps = [
        ("ruff check", ["-m", "ruff", "check", "."]),
        ("ruff format --check", ["-m", "ruff", "format", "--check", "."]),
    ]
    if not fast:
        steps.append(("mypy", ["-m", "mypy"]))
    steps.append(("pytest", pytest_args))
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="项目统一质量检查")
    parser.add_argument("--fast", action="store_true", help="只跑规则、ruff 与单元测试")
    parser.add_argument("--live", action="store_true", help="额外运行 live 测试")
    args = parser.parse_args(argv)

    report = Report()
    check_files(report)
    check_tests_mirror(report)
    check_tracked(report)
    print_report(report)
    if report.failed:
        print("规则检查未通过，已终止")
        return 1

    for name, step_args in quality_steps(args.fast, args.live):
        if not run_step(name, step_args):
            print(f"\n检查失败: {name}")
            return 1
    print("\n全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
