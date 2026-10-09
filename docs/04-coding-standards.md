# 04 代码规范

## 1. 模块头注释（强制，CI 校验）

每个源码文件第一段必须是模块说明，至少包含 `模块:` 与 `职责:`。

```python
"""
模块: gateway.qqbot
职责: QQ 官方机器人适配器——鉴权、收消息、发消息、断线重连
依赖: gateway.base, core.config, core.log
来源: 移植自 hermes-agent@<commit> gateway/platforms/qqbot/ (MIT)，删除富媒体与多账号
"""
```

- `依赖:` 列出直接依赖的内部模块。
- `来源:` 仅移植代码需要，写明仓库、commit、原路径、协议、删改要点。
- TypeScript：`/** 模块: ... 职责: ... */`；Vue：首行 `<!-- 模块: ... 职责: ... -->`。

## 2. 文件行数

| 行数 | 处理 |
| --- | --- |
| ≤ 700 | 正常 |
| 701–1400 | 允许，PR 描述中说明原因（CI 警告） |
| > 1400 | 禁止合入（CI 失败） |

**禁止为规避行数拆文件**：同一模块 / 同一逻辑不得拆成 `xxx_part2.py`、`xxx_ext.py`、mixin 拼装、`xxx_helpers.py` 等多个文件。
超限时依次处理：① 删冗余、复用已有函数；② 若确实存在两个独立职责，则按职责拆成两个模块，并更新 [02-structure.md](02-structure.md)。

## 3. 复用与抽象

- 新写函数前先全局搜索，已有能力必须复用；两处以上重复逻辑必须收敛。
- 共享能力只放 `core/`，按职责命名。
- 只有 ≥ 2 个实现时才允许抽象基类 / Protocol（例：`BaseAdapter` 有微信、QQ 两个实现）。
- 不写只转发一次调用的包装函数；不建工厂、注册表，除非已有 ≥ 3 个实现。
- 继承不超过 2 层。

## 4. 命名

- 函数名不超过 3 个单词、约 25 个字符：`send_text`、`load_schedules`、`build_report`。
  反例：`process_and_send_formatted_message_to_target_platform`。
- 禁止空泛命名：`Manager`、`Helper`、`Util`、`Processor`、`do_xxx`、`handle_data`。
- 类名用名词，函数名用动词开头，布尔用 `is_` / `has_`。
- 常量全大写，放在模块顶部；可调参数（超时、URL、并发数）放配置，禁止硬编码。

## 5. 函数与结构

- 单函数建议 ≤ 60 行，参数 ≤ 5 个（超过用 dataclass）。
- 公共函数必须有类型注解；mypy 不得新增错误。
- 跨模块只调用对方的公共名字（不以 `_` 开头）。
- 遵守依赖方向：`api, gateway → dispatcher → agent, result → core`。

## 6. 异步与 IO

- 所有网络 / 数据库 IO 使用 async；阻塞调用用 `asyncio.to_thread`。
- 每个外部调用必须设置超时。
- 后台协程必须可取消，并在关闭时回收（浏览器进程尤其要关闭）。

## 7. 错误处理与日志

- 禁止裸 `except:` 和吞异常；只在边界（worker、适配器收消息循环、API 层）统一捕获。
- 错误类型统一定义在 `core/errors.py`：`RetriableError`、`FatalError`、`AuthError` 等。
- 使用 `core.log.get_logger(__name__)`，禁止 `print`。日志不得输出密钥、完整 Cookie、用户隐私。

## 8. 注释

- 只写代码本身表达不出的内容（原因、约束、坑），一行为宜。
- 修复 bug 时，若原因无法从代码看出，在修复处加一行注释说明原因。
- 不写复述代码的注释，不保留注释掉的旧代码。

## 9. 安全清单（每个 PR 自查）

- [ ] 无硬编码密钥，配置走 `settings`
- [ ] 外部输入（聊天消息、网页内容、API 参数）已校验，网页内容不作为指令执行
- [ ] 抓取类请求有 SSRF 拦截
- [ ] 数据库只用 ORM / 参数化
- [ ] 新接口有鉴权

## 10. 前端

- TypeScript `strict`；Composition API + `<script setup>`。
- HTTP 请求只能写在 `web/src/api/`。
- 一个页面一个文件；组件 ≥ 2 处使用才提取到 `components/`。
- ESLint + Prettier 由 `scripts/check.py` 调用。
