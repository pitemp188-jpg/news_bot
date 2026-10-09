# 05 自动化测试

## 1. 测试分层

| 层级 | 目录 | 标记 | 外部依赖 | 运行时机 |
| --- | --- | --- | --- | --- |
| 单元 | `tests/unit/` | 默认 | 全部替身 | 每次提交（pre-commit `--fast`）、CI |
| 集成 | `tests/integration/` | `integration` | 真实 SQLite、本地网页、Fake 平台 / LLM | CI |
| 端到端 | `tests/e2e/` | `e2e` | 全链路，Fake 平台 + Fake LLM + 本地网页 + 真实浏览器 | CI（PR 合入前）|
| 真实服务 | 任意 | `live` | 真实 LLM / 搜索 / 微信 / QQ | 手动或每日定时，需密钥 |
| 浸泡 | `tests/e2e/test_soak.py` | `soak` | 同 e2e | 手动，发版前 |

默认 `pytest` 只运行未标记 + `integration` + `e2e`；`live`、`soak` 需显式 `-m`。

## 2. 测试替身（`tests/fakes/`）

| 替身 | 作用 |
| --- | --- |
| `llm.py` · `FakeLLM` | 按脚本依次返回预设回复 / tool_call，校验调用次数 |
| `adapter.py` · `FakeAdapter` | 模拟平台收发，记录发送内容，可注入断线 |
| `search.py` · `FakeSearch` | 返回固定搜索结果，指向 `tests/fixtures/pages/` |
| `search.py` · `FakeBrowserRunner` | 浏览器子 Agent 替身，记录调用与并发峰值 |

外部 HTTP 统一用 `respx` mock，**单元测试禁止真实联网**（`conftest.py` 中全局禁用 socket）。
`local_server` fixture 在回环地址上提供 `tests/fixtures/pages/`，供集成与 e2e 做真实抓取 / 浏览器访问；`NEWSBOT_DATA_DIR` 指向临时目录，避免污染 `data/`。

## 3. 覆盖率

- `core`、`dispatcher`、`result`、`gateway/router.py`、`gateway/auth.py`：≥ 85%
- 其余模块：≥ 70%
- CI 用 `pytest --cov` 校验，低于阈值失败。

## 4. 编写规则

1. 每个任务必须同时提交测试；没有测试的任务不算完成。
2. 修 bug 必须先写一个**能复现该 bug 的失败用例**，修复后转绿，作为回归测试保留。
3. 禁止为了通过而删除断言、放宽断言、加 `skip` / `xfail`；确需跳过须在 PR 中说明并登记阻塞记录。
4. 测试命名：`test_<被测行为>_<条件>`，如 `test_send_retries_on_timeout`。
5. 时间相关用可注入时钟（freezegun / 参数传入 now），禁止真实 `sleep` 等待。
6. 浏览器测试只访问 `tests/fixtures/pages/` 的本地页面（由 fixture 启动本地 HTTP 服务）。
7. 里程碑级验证按第 5 节执行：必须跑 e2e，界面类改动必须用内置浏览器实际打开、点击并截图确认。

## 5. 端到端与浏览器验收（强制）

单元测试通过**不等于**功能可用。以下三条为硬性要求，任何里程碑都不能用“单元测试全绿”代替：

1. **每个里程碑必须有该里程碑可达的最高层级验证**：链路尚未打通时（M1–M5）用 `tests/integration/` 跑真实数据库 / 本地站点；M6 起必须用 `tests/e2e/`（标记 `e2e`）以 Fake 平台 + Fake LLM + 本地站点跑通完整链路。里程碑提 PR 前必须在本地实际运行过一次，并在 PR 中贴出结果。
2. **涉及界面的功能必须用 VS Code 内置浏览器实际打开验证**：打开页面 → 点击 / 输入 → 读取页面状态 → 截图确认，能观察到期望结果是唯一判据。不允许只看接口返回或模块单测就宣称界面没问题。
3. **涉及网页采集的功能必须用真实浏览器访问本地站点验证**：`tests/fixtures/pages/` 由 fixture 起本地 HTTP 服务，`fetch` / `browse` 走真实抓取路径；页面改版或抽取逻辑变更后要重新验证 `tests/fixtures/pages/`。

验收记录：

- 截图或验证结论写入 PR 描述；
- 同时在 [03-tasks.md](03-tasks.md) 的“长跑记录”里写明“已用内置浏览器验证 <页面/链路>”。

当界面或链路行为与测试不符时，按 [06-git-workflow.md](06-git-workflow.md) 第 4 节先补一个能复现的用例，再修复并重新截图复验。

## 6. 统一入口 `scripts/check.py`

```
python scripts/check.py            # 全量：规则 + ruff + mypy + pytest(含覆盖率) + 前端检查
python scripts/check.py --fast     # 快速：规则 + ruff + 单元测试（pre-commit 使用）
python scripts/check.py --live     # 额外运行 live 测试
```

执行顺序（任一步失败即停止并返回非 0）：

1. 规则检查：行数、模块头注释、目录白名单、禁用文件名、import 方向、疑似密钥
2. `ruff check` + `ruff format --check`
3. `mypy src`
4. `pytest`（含覆盖率阈值）
5. `web/` 存在时：`npm run lint` + `npm run test`

## 7. CI（`.github/workflows/ci.yml`）

| 触发 | 任务 |
| --- | --- |
| push 任意分支 / PR | `python scripts/check.py` |
| 每日 03:00 定时 | `python scripts/check.py --live`（使用仓库 Secrets；未配置密钥时自动跳过） |
| 打 tag | 全量检查 + 构建 Docker 镜像（随 T9.1 落地） |

CI 的 job 名为 `check`，分支保护里选择该检查名。

CI 失败时：

- 发生在功能分支：由该分支开发者 / 代理修复后再推送。
- 发生在 `main` 或每日定时任务：视为 bug，按 [06-git-workflow.md](06-git-workflow.md) 第 4 节开 `fix/` 分支并提 PR.
