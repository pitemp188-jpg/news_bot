# 03 模块开发任务

> 本文件是自动化开发的**唯一任务源**。开发代理按顺序领取任务，完成后更新状态。
> 状态：`[ ]` 未开始　`[~]` 进行中　`[x]` 完成　`[!]` 阻塞（须在“阻塞记录”写明原因）
> 每个任务的规模控制在一次长跑会话可完成（新增代码约 ≤ 500 行 + 测试）。
> 每个任务完成即 commit + push 到模块分支；每个里程碑完成即提 PR 合入 `main` 并打 tag。

## M0 工程基建　分支 `feat/m0-bootstrap`　tag `v0.0.1`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [x] | T0.1 | 仓库初始化 | `.gitignore`、`.gitattributes`(LF)、`.editorconfig`、远程 `origin` | 首次推送 `main` 成功；`.env`、`data/` 被忽略 | — |
| [x] | T0.2 | Python 工程 | `pyproject.toml`、`src/newsbot/__init__.py`、`.env.example`、`config.example.yaml` | `uv sync` 成功；`ruff`、`mypy`、`pytest` 均可运行 | T0.1 |
| [ ] | T0.3 | 规则检查脚本 | `scripts/check.py` | 能检测：行数(>700 警告 / >1400 失败)、模块头注释、目录白名单、禁用文件名、import 方向、疑似密钥；串联 ruff/mypy/pytest；自身有单元测试 | T0.2 |
| [ ] | T0.4 | Git hooks | `scripts/hooks/pre-commit`、`scripts/hooks/commit-msg` | pre-commit 跑 `check.py --fast`；commit-msg 校验提交格式（见 06） | T0.3 |
| [ ] | T0.5 | CI | `.github/workflows/ci.yml` | PR 与 push 触发 `check.py`；失败阻止合并 | T0.3 |

## M1 核心层　分支 `feat/m1-core`　tag `v0.1.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T1.1 | 配置 | `core/config.py` | pydantic-settings 读取 `.env` + `config.yaml`；缺必填项启动报错 | M0 |
| [ ] | T1.2 | 日志 | `core/log.py` | 结构化日志、trace id、密钥脱敏、按天轮转到 `data/logs` | T1.1 |
| [ ] | T1.3 | 数据库 | `core/db.py`、`core/models.py`、`migrations/` | 01 文档全部表建好；Alembic 首个迁移可升级/回滚 | T1.1 |
| [ ] | T1.4 | 错误与 LLM | `core/errors.py`、`core/llm.py` | OpenAI 兼容调用、tool-calling、重试、token 记录到 `llm_usage` | T1.3 |

## M2 消息网关（移植 hermes）　分支 `feat/m2-gateway`　tag `v0.2.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [~] | T2.1 | 移植调研 | `docs/08-porting.md` 第 3、4、7 节（已完成）、`THIRD_PARTY_NOTICES.md`（待建） | 锁定 hermes commit；协议、依赖、取舍已写清 | M1 |
| [ ] | T2.2 | 适配器基类 | `gateway/base.py`、`tests/fakes/adapter.py` | 按 08 第 4.2 节契约实现；`split_text` 保持代码块完整；去重 TTL 生效；FakeAdapter 可收发 | T2.1 |
| [ ] | T2.3 | QQ 适配器 | `gateway/qqbot.py`、`tests/fakes/qq_gateway.py` | token 提前刷新；Hello→Identify→READY→心跳→断线 Resume；08 第 3.3 节全部关闭码分支有测试；`live` 测试确认主动消息额度 | T2.2 |
| [ ] | T2.4 | 微信适配器 | `gateway/weixin.py`、`login` 子命令 | 扫码登录；游标与 context_token 持久化到 `data/weixin/`；-14 / -2 / 限流熔断分支有测试；`live` 测试手动通过 | T2.2 |
| [ ] | T2.5 | 鉴权 | `gateway/auth.py` | 白名单 + 配对码；未授权消息忽略并记录 | T2.2 |
| [ ] | T2.6 | 路由与投递 | `gateway/router.py` | 入站→鉴权→指令/任务；出站分段、重试 3 次、写 `delivery` 表；`need_user` 时记 `waiting_user` 并在用户下次发消息时补投；主通道失败切备用通道 | T2.5 |

## M3 任务调度中心　分支 `feat/m3-dispatcher`　tag `v0.3.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T3.1 | 任务队列 | `dispatcher/queue.py` | 提交/领取/并发上限/超时/重试/取消/重启恢复，状态机符合 01 第 7 节 | M1 |
| [ ] | T3.2 | 会话 | `dispatcher/session.py` | 按 (platform, chat_id) 保存最近 N 轮，超长截断 | T3.1 |
| [ ] | T3.3 | 定时调度 | `dispatcher/scheduler.py` | 从 `schedule` 表同步 job；运行时增删；错过触发容忍；时区正确 | T3.1 |
| [ ] | T3.4 | 执行流水线 | `dispatcher/pipeline.py` | agent→result→Sender；用 Fake 全替身跑通 | T3.1 |

## M4 AI Agent　分支 `feat/m4-agent`　tag `v0.4.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T4.1 | 搜索工具 | `agent/tools/search.py` | SearXNG / Tavily 统一返回结构；时间范围过滤；respx mock 测试 | M1 |
| [ ] | T4.2 | 抓取工具 | `agent/tools/fetch.py` | 正文抽取、大小上限、超时、SSRF 拦截（内网/回环/非 http） | M1 |
| [ ] | T4.3 | 浏览器工具 | `agent/tools/browser.py` | 封装 browser-use；步数/超时上限；并发信号量；本地 fixture 页面测试 | M1 |
| [ ] | T4.4 | 资讯库工具 | `agent/tools/newsdb.py` | 按关键词 / 时间查询 `news_item` | M1 |
| [ ] | T4.5 | 规划循环 | `agent/runner.py`、`agent/prompts.py` | tool-calling 循环；步数/token/耗时预算；输出带来源的 findings；FakeLLM 脚本化测试 | T4.1–T4.4 |

## M5 结果处理　分支 `feat/m5-result`　tag `v0.5.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T5.1 | 去重 | `result/dedup.py` | URL 归一化、内容哈希、simhash 近似去重、对比近 7 天已推送 | M1 |
| [ ] | T5.2 | 成稿 | `result/report.py` | LLM 摘要、引用编号 [1][2]、来源列表、按平台转纯文本并分段 | T5.1 |

## M6 场景联调　分支 `feat/m6-scenarios`　tag `v0.6.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T6.1 | 聊天指令 | `gateway/commands.py` | 01 指令表全部可用；查询任务即时回执 | M2–M5 |
| [ ] | T6.2 | 应用组装 | `app.py`、`__main__.py` | `python -m newsbot run` 启动全部组件；Ctrl+C 优雅退出 | T6.1 |
| [ ] | T6.3 | 定时推送 | 默认 21:00 日报配置 | 时间快进测试触发并推送到 FakeAdapter | T6.2 |
| [ ] | T6.4 | 端到端 | `tests/e2e/` | S1、S2 两场景全链路（Fake 平台 + Fake LLM + 本地网页）通过 | T6.3 |

## M7 管理 API　分支 `feat/m7-api`　tag `v0.7.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T7.1 | 服务与登录 | `api/server.py` | 默认监听 127.0.0.1；口令登录；HttpOnly Cookie；CSRF 校验 | M6 |
| [ ] | T7.2 | 业务接口 | `api/routes/*.py` | 任务列表/详情/取消/重试；定时任务 CRUD；资讯与推送记录查询；系统状态（适配器在线、队列长度、今日 token） | T7.1 |

## M8 管理界面　分支 `feat/m8-web`　tag `v0.8.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T8.1 | 前端脚手架 | `web/` | Vue3 + Vite + TS + Naive UI；登录页；api 封装 | M7 |
| [ ] | T8.2 | 页面 | `web/src/views/*` | 仪表盘、任务、定时任务、资讯库、推送记录、设置；vitest 通过 | T8.1 |
| [ ] | T8.3 | 托管 | `api/server.py` 静态挂载 | `npm run build` 后由 FastAPI 提供访问 | T8.2 |

## M9 部署运维　分支 `feat/m9-deploy`　tag `v0.9.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T9.1 | 容器化 | `docker/Dockerfile`、`docker/compose.yaml` | 含 Chromium；`data/` 挂卷；一条命令启动 | M8 |
| [ ] | T9.2 | 运维 | 健康检查接口、备份命令 | `/api/health`；`python -m newsbot backup` 生成数据库快照 | T9.1 |
| [ ] | T9.3 | 开发通知 | `python -m newsbot notify "<文本>"` | 长跑结束可把总结推送到你的微信/QQ | T6.2 |

## M10 稳定性　分支 `feat/m10-hardening`　tag `v1.0.0`

| 状态 | 编号 | 任务 | 产出 | 验收标准 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| [ ] | T10.1 | 故障演练 | `tests/integration/test_faults.py` | LLM 超时、适配器断线、浏览器崩溃、数据库锁均能恢复 | M9 |
| [ ] | T10.2 | 浸泡测试 | `tests/e2e/test_soak.py`（标记 `soak`） | 24h 连续运行无内存泄漏、无僵尸浏览器进程 | T10.1 |

---

## 阻塞记录

| 日期 | 任务 | 原因 | 需要谁处理 |
| --- | --- | --- | --- |

## 长跑记录

| 日期 | 代理 | 完成任务 | 分支 / PR | 测试结果 | 遗留问题 |
| --- | --- | --- | --- | --- | --- |
