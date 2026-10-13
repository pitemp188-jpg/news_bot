"""
模块: tests.integration.test_watchdog
职责: 看护脚本的契约检查——它必须拉起 `api`（机器人 + 网页的超集），且只认一种进程
依赖: scripts/watchdog.ps1
"""

from __future__ import annotations

import re
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[2] / "scripts" / "watchdog.ps1"


def _text() -> str:
    return WATCHDOG.read_text(encoding="utf-8")


def test_watchdog_launches_the_api_entry_point() -> None:
    """拉起 `api` 而不是 `run`。

    `api` 是 `run` 的超集：一个进程同时提供机器人与网页。两个都起会各挂一个 30 分钟
    事件层刷新作业（实测撞出 `UNIQUE constraint failed: story_score.news_item_id`、
    整批评分丢失），还会互抢同一个 QQ 会话。
    """
    text = _text()
    launch = re.search(r"-ArgumentList([^\n]*)", text)
    assert launch is not None, "找不到启动参数行"
    # 只取引号里的词，避开行尾的续行反引号
    args = re.findall(r"'([^']*)'", launch.group(1))
    assert args == ["run", "python", "-m", "newsbot", "api"], f"启动参数应为 uv run python -m newsbot api，实际 {args}"


def test_watchdog_matches_the_api_process_not_run() -> None:
    """存活判定必须与新启动方式一致，否则会**反复拉起**：匹配不到自己刚起的进程，
    每个检查周期都再起一个，几分钟内堆出一堆实例（正是并发写库冲突的来源）。"""
    text = _text()
    patterns = re.findall(r"CommandLine -match '([^']+)'", text)
    assert patterns, "找不到进程匹配规则"
    joined = " ".join(patterns)
    assert r"newsbot\s+api" in joined, f"应匹配 api 进程，实际规则 {patterns}"
    assert r"newsbot\s+run" not in joined, "匹配规则里不该再留着 run（会造成重复拉起）"


def test_watchdog_covers_the_single_instance_reason() -> None:
    """把「为什么只能是单实例」写在脚本里，避免后人改回去。

    这条不是形式主义：两个实例并发写库这件事**真的发生过**（story_score 唯一约束
    冲突、整批评分丢失），不写清原因的话"顺手加个 run 一起起"看起来毫无害处。
    """
    text = _text()
    assert "超集" in text, "应说明 api 是 run 的超集"
    assert "story_score" in text or "唯一约束" in text, "应说明多实例会撞唯一约束"
    assert "会话" in text, "应说明多实例会互抢 QQ 会话"
