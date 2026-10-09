# news_bot

AI 驱动的资讯机器人：由大模型调度搜索引擎与浏览器（browser-use）采集信息、汇总去重并附来源，推送到微信 / QQ。

- **定时推送**：每天 21:00（可配置）按订阅主题推送日报。
- **聊天指令**：在微信 / QQ 发消息给 bot，后台搜索后回复结果，支持追问。

两种场景共用同一个 Agent 引擎，只是触发方式不同。

## 文档

| 文档 | 内容 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 开发代理必读规则 |
| [docs/01-architecture.md](docs/01-architecture.md) | 架构、技术选型、模块、数据模型、安全 |
| [docs/02-structure.md](docs/02-structure.md) | 目录规范 |
| [docs/03-tasks.md](docs/03-tasks.md) | 里程碑与任务清单（自动化开发任务源） |
| [docs/04-coding-standards.md](docs/04-coding-standards.md) | 代码规范 |
| [docs/05-testing.md](docs/05-testing.md) | 自动化测试 |
| [docs/06-git-workflow.md](docs/06-git-workflow.md) | 分支、提交、推送、PR 规则 |
| [docs/07-autonomous-dev.md](docs/07-autonomous-dev.md) | 自动化长跑开发流程 |
| [docs/08-porting.md](docs/08-porting.md) | hermes 网关移植与第三方协议 |

## 状态

规划阶段，开发从 [docs/03-tasks.md](docs/03-tasks.md) 的 M0 开始。
