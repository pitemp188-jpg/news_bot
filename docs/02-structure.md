# 02 目录规范

## 1. 目录结构（唯一合法结构）

```
news_bot/
├── AGENTS.md                    # AI 开发代理必读规则入口
├── README.md
├── THIRD_PARTY_NOTICES.md       # 第三方许可声明（移植代码必须登记）
├── pyproject.toml / uv.lock
├── alembic.ini
├── config.example.yaml          # 非敏感配置样例
├── .env.example                 # 密钥样例（真实 .env 不入库）
├── .dockerignore                # docker 构建上下文排除项
├── .github/
│   ├── workflows/ci.yml
│   └── pull_request_template.md
├── docker/
│   ├── Dockerfile               # 三段式构建：前端产物 → Python 依赖 → 运行时（含 Chromium）
│   └── compose.yaml             # 一条命令拉起服务，data 用命名卷
├── docs/                        # 规划与规范文档（编号命名）
├── migrations/                  # Alembic 迁移脚本
├── scripts/
│   ├── check.py                 # 统一质量检查入口（lint/类型/规则/测试）
│   ├── watchdog.ps1             # 进程看护：服务掉了就拉起（长跑用）
│   └── hooks/                   # git hooks：pre-commit、commit-msg
├── src/newsbot/
│   ├── __main__.py              # CLI：run / login / notify / migrate
│   ├── app.py                   # 组装依赖、启动与优雅关闭
│   ├── core/
│   │   ├── config.py            # 配置加载
│   │   ├── log.py               # 日志与脱敏
│   │   ├── db.py                # 引擎与会话
│   │   ├── models.py            # 全部 ORM 模型
│   │   ├── errors.py            # 错误类型
│   │   └── llm.py               # LLM 客户端（agent 与 result 共用）
│   ├── gateway/
│   │   ├── base.py              # MessageEvent、BaseAdapter、分段工具
│   │   ├── weixin.py            # 微信适配器
│   │   ├── qqbot.py             # QQ 官方机器人适配器
│   │   ├── auth.py              # 白名单与配对
│   │   ├── commands.py          # 聊天指令解析与执行
│   │   └── router.py            # 入站路由、出站投递
│   ├── dispatcher/
│   │   ├── queue.py             # 任务队列、worker、超时重试取消、恢复
│   │   ├── session.py           # 会话上下文
│   │   ├── scheduler.py         # 定时调度
│   │   └── pipeline.py          # 执行流水线 agent→result→send
│   ├── agent/
│   │   ├── runner.py            # 规划循环
│   │   ├── prompts.py           # Agent 提示词
│   │   └── tools/
│   │       ├── base.py          # Tool 协议与 Source / ToolResult 类型
│   │       ├── search.py
│   │       ├── fetch.py
│   │       ├── browser.py
│   │       └── newsdb.py
│   ├── result/
│   │   ├── dedup.py
│   │   └── report.py
│   └── api/
│       ├── server.py            # FastAPI 应用、登录、静态资源托管
│       ├── security.py          # 会话与 CSRF
│       ├── deps.py              # 依赖注入与鉴权依赖
│       ├── serializers.py       # ORM 行 → JSON
│       └── routes/
│           ├── tasks.py
│           ├── schedules.py
│           ├── news.py
│           └── system.py
├── web/                         # 管理界面
│   ├── package.json / vite.config.ts / tsconfig.json / eslint.config.js
│   └── src/
│       ├── main.ts / App.vue / router.ts / styles.css
│       ├── api/                 # 唯一允许发 HTTP 请求的目录（client.ts、format.ts）
│       ├── views/               # 页面，一页一个文件
│       └── components/          # 复用组件
├── tests/
│   ├── conftest.py
│   ├── fakes/                   # FakeLLM、FakeAdapter、FakeSearch、FakeQQGateway
│   ├── fixtures/pages/          # 本地静态网页，供 fetch/browser 测试
│   ├── unit/                    # 镜像 src/newsbot 目录结构
│   ├── integration/
│   ├── e2e/
│   └── live/                    # 真实模型 / 真实抓取（标记 live，默认不跑）
└── data/                        # 运行时数据：db、日志、浏览器 profile、weixin/ 凭证与游标（gitignore）
```

## 2. 放置规则

1. **新文件只能放进上表已定义的目录**。确需新增目录或新模块文件，必须在同一个 commit 中更新本文档，并在提交说明里写明理由。
2. 单元测试路径镜像源码：`src/newsbot/gateway/router.py` → `tests/unit/gateway/test_router.py`。
3. 禁止杂物文件：`utils.py`、`common.py`、`helpers.py`、`misc.py`、`tmp_*.py`。共享能力只能放 `core/`，且按职责命名。
4. 测试替身只放 `tests/fakes/`，测试网页只放 `tests/fixtures/pages/`，不得散落在测试文件中。
5. 运行时产物（数据库、日志、截图、浏览器缓存）只能写入 `data/`。
6. 临时调试脚本不得提交；需要长期保留的脚本放 `scripts/` 并加模块头注释。
7. 文件命名：Python `snake_case.py`；Vue 组件 `PascalCase.vue`；TS 模块 `camelCase.ts`；文档 `NN-kebab-case.md`。

## 3. 由 `scripts/check.py` 自动校验

- 源码文件是否位于白名单目录。
- 是否出现禁用文件名。
- `tests/unit` 是否与 `src/newsbot` 对应（缺测试文件给出警告）。
