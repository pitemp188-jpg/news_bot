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

已完成 M0–M9（脚手架、网关、队列、Agent、成稿、场景、真实链路联调、管理 API、管理界面、部署运维），当前进度见 [docs/03-tasks.md](docs/03-tasks.md)。

## 部署

### Docker Compose（推荐）

```bash
cp .env.example .env          # 至少填 LLM_API_KEY 与 ADMIN_PASSWORD
docker compose -f docker/compose.yaml up -d --build
```

管理后台：<http://127.0.0.1:8765>　健康检查：`GET /api/health`

- 镜像三段式构建：node 阶段产出 `web/dist`，builder 阶段装 Python 依赖，运行阶段只留 venv 与 Chromium。
- 数据（数据库、日志、浏览器 profile、微信凭证）都在命名卷 `newsbot-data`，映射到容器内 `/app/data`，重建容器不丢。
- 容器内以非 root 用户运行；Chromium 与中文字体已预装，供浏览器子 Agent 抓取动态中文页面。
- 改监听端口时，Dockerfile 里 `CMD` 的 `--port` 与 `HEALTHCHECK` 的地址要一起改。

### 本地直跑

```bash
uv sync                                # 需要浏览器兜底再加 --extra browser
python -m newsbot doctor               # 离线自检：配置、凭证、可选依赖
python -m newsbot run                  # 机器人服务
python -m newsbot api                  # 管理后台
python -m newsbot backup               # 数据库快照，输出到 data/backups/
python -m newsbot notify "部署完成"     # 长跑结束给自己发一条
```
