"""
模块: tests.e2e.test_soak
职责: 浸泡测试——长时间连续跑完整链路，观测内存增长、任务泄漏与浏览器进程残留
依赖: newsbot.app, tests.fakes

默认跑 24 小时，只在发版前手动执行（`-m soak`）。本地快速验证用环境变量缩短：
  NEWSBOT_SOAK_SECONDS=60 uv run python -m pytest -m soak tests/e2e/test_soak.py -s
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.fakes.adapter import FakeAdapter

from newsbot.app import App
from newsbot.core.config import Config, ScheduleSection, Secrets
from newsbot.core.llm import LLMReply
from newsbot.core.models import Delivery, Task

pytestmark = [pytest.mark.e2e, pytest.mark.soak]

ACTIVE_STATES = ("pending", "running")


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


# 默认 24h；下面三个都可以用环境变量覆盖，便于短跑验证
DURATION = _number("NEWSBOT_SOAK_SECONDS", 24 * 3600)
SAMPLE_INTERVAL = _number("NEWSBOT_SOAK_INTERVAL", 60.0)
MAX_GROWTH_MB = _number("NEWSBOT_SOAK_MAX_GROWTH_MB", 96.0)
# 任务数不变时，存活 asyncio 任务允许的抖动（队列 worker、调度器等）
TASK_SLACK = 6


@dataclass
class SteadyLLM:
    """永远返回同一条答复——浸泡测试关心资源曲线，不关心内容多样性。"""

    calls: int = 0

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        self.calls += 1
        return LLMReply(content="浸泡测试答复：链路正常 [1]", prompt_tokens=5, completion_tokens=5)

    async def aclose(self) -> None:
        return None


@dataclass
class TrackingBrowser:
    """浏览器替身：记录被创建与被关闭，用来发现残留的浏览器运行器。"""

    runs: int = 0
    closed: int = 0

    async def run(self, url: str, task: str) -> str:
        self.runs += 1
        return "页面正文"

    async def aclose(self) -> None:
        self.closed += 1


def _config(tmp_path: Path) -> Config:
    secrets = Secrets(_env_file=None, llm_api_key="sk-test", weixin_allowed_users="u1")
    # 关掉去重：反复跑同一句话时不该被判成旧闻，否则测的是去重而不是资源
    return Config(secrets=secrets, schedule=ScheduleSection(dedup_days=0), data_dir=tmp_path)


def _rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / 1024 / 1024


def _browser_children() -> int:
    """统计当前进程下名字含 chrome/chromium 的子进程数（无浏览器依赖时为 0）。"""
    import psutil

    me = psutil.Process()
    try:
        children = me.children(recursive=True)
    except Exception:  # pragma: no cover - 平台差异
        return 0
    return sum(1 for child in children if "chrom" in child.name().lower())


async def _drain(app: App, adapter: FakeAdapter, timeout: float = 60.0) -> int:
    """等当前批次跑完并返回本轮投递条数；顺手清空 adapter 记录，避免测试自身占内存。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    assert app.db is not None and app.queue is not None
    while loop.time() < deadline:
        async with app.db.session() as session:
            rows = list((await session.execute(select(Task))).scalars())
        busy = any(row.status in ACTIVE_STATES for row in rows)
        if not busy and app.queue.depth == 0 and app.queue.running == 0:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - 仅在被测系统卡死时触发
        pytest.fail(f"{timeout}s 内任务未收敛，队列深度 {app.queue.depth}，执行中 {app.queue.running}")
    sent = len(adapter.sent)
    adapter.sent.clear()
    return sent


async def test_soak_full_chain_stays_stable(tmp_path: Path) -> None:
    psutil = pytest.importorskip("psutil")
    assert psutil is not None

    adapter = FakeAdapter(platform="weixin")
    browser = TrackingBrowser()
    llm = SteadyLLM()
    app = App(_config(tmp_path), llm=llm, adapters=[adapter], browser=browser)

    await app.start()
    samples: list[float] = []
    baseline_rss = 0.0
    baseline_tasks = 0
    processed = 0
    sent_total = 0
    try:
        # 预热：让导入、连接池、缓存先稳定下来，否则前几轮的 RSS 会被初始化开销抬高
        for index in range(3):
            await adapter.emit(f"预热查询 {index}", chat_id="u1", user_id="u1")
            sent_total += await _drain(app, adapter)
            processed += 1

        baseline_rss = _rss_mb()
        baseline_tasks = len(asyncio.all_tasks())
        baseline_children = _browser_children()
        samples.append(baseline_rss)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + DURATION
        next_sample = loop.time() + SAMPLE_INTERVAL

        while loop.time() < deadline:
            await adapter.emit(f"第 {processed + 1} 轮查询", chat_id="u1", user_id="u1")
            sent_total += await _drain(app, adapter)
            processed += 1
            if loop.time() >= next_sample:
                samples.append(_rss_mb())
                print(
                    f"[soak] 轮次 {processed} 常驻内存 {samples[-1]:.1f}MB "
                    f"(相对基线 {samples[-1] - baseline_rss:+.1f}MB) "
                    f"存活任务 {len(asyncio.all_tasks())}",
                    flush=True,
                )
                next_sample = loop.time() + SAMPLE_INTERVAL

        final_rss = _rss_mb()
        samples.append(final_rss)

        # 1. 确实跑了活：空转通过等于没测
        assert processed > 3, f"只跑完 {processed} 轮，几乎没产生负载"
        assert sent_total >= processed - 3, f"投递数 {sent_total} 明显少于轮次 {processed}"

        # 2. 内存不泄漏：全程峰值相对基线的增量要受控
        growth = max(samples) - baseline_rss
        assert growth <= MAX_GROWTH_MB, f"常驻内存增长 {growth:.1f}MB，超过上限 {MAX_GROWTH_MB}MB"

        # 3. 不留僵尸 asyncio 任务
        live = len(asyncio.all_tasks())
        assert live <= baseline_tasks + TASK_SLACK, f"存活 asyncio 任务从 {baseline_tasks} 涨到 {live}"

        # 4. 不留僵尸浏览器进程：子进程数不增长，且运行器被真正关闭
        assert _browser_children() <= baseline_children, "退出前仍残留 chrome/chromium 子进程"

        # 5. 队列收尾干净，没有卡在中间态的任务
        assert app.queue is not None
        assert app.queue.depth == 0 and app.queue.running == 0
        async with app.db.session() as session:  # type: ignore[union-attr]
            leftovers = [
                row.id for row in (await session.execute(select(Task))).scalars() if row.status in ACTIVE_STATES
            ]
            deliveries = list((await session.execute(select(Delivery))).scalars())
        assert leftovers == [], f"仍有未收敛的任务: {leftovers}"
        assert deliveries and all(row.status == "sent" for row in deliveries)
    finally:
        await app.stop()

    # 关闭之后再确认一次：工具被回收、没有残留浏览器运行器
    assert browser.closed == 1, "服务关闭时必须回收浏览器运行器，否则会留下僵尸进程"
    assert _browser_children() == 0, "服务关闭后仍有 chrome/chromium 子进程残留"
    print(
        f"[soak] 完成 {processed} 轮，采样 {len(samples)} 次，内存增量 {samples[-1] - baseline_rss:+.1f}MB", flush=True
    )
