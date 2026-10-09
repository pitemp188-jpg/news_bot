# 08 第三方代码复用与移植

## 1. 来源与协议

| 项目 | 用途 | 协议 | 方式 |
| --- | --- | --- | --- |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 消息网关：`gateway/platforms/` 下的 `base.py`、`event.py`、`weixin.py`、`qqbot/`，及 `pairing.py`、`delivery.py` 的思路 | MIT | 裁剪移植（拷贝后修改） |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | 浏览器 Agent 内核 | MIT | pip 依赖，不拷贝源码 |
| SearXNG | 搜索 | AGPL-3.0 | 独立部署，仅通过 HTTP 调用，不拷贝代码 |

MIT 协议要求保留版权声明：所有移植代码在 `THIRD_PARTY_NOTICES.md` 中登记并附原 LICENSE 全文，文件头 `来源:` 字段写明出处。

## 2. 原则

1. **依赖优先，拷贝其次**：能 `pip install` 的不拷贝（browser-use）；只有需要深度修改的才拷贝（网关）。
2. **只取需要的部分**：hermes 网关规模很大（上百个文件，耦合 `hermes_state`、cron、插件、i18n、hosted rooms、kanban、语音、流式输出等），本项目只取微信、QQ 收发所需部分。
3. **按本项目规范重组**：hermes 将 `GatewayRunner` 拆成大量 `run_*.py` mixin，这与本项目“禁止为规避行数拆文件”冲突。移植时按职责合并为 [02-structure.md](02-structure.md) 规定的文件。
4. **替换基础设施**：hermes 的配置、日志、状态库调用一律替换为 `core.config`、`core.log`、`core.db`。
5. **锁定版本**：记录移植时的 commit，后续上游修复按 diff 手动同步。

## 3. hermes 网关实现分析

> 调研基于 hermes-agent `main@908e4a4`（2026-10-09）。正式移植（T2.2–T2.4）时复核 commit 并更新第 7 节。

### 3.1 整体结构

| hermes 文件 | 规模 | 作用 |
| --- | --- | --- |
| `gateway/platforms/event.py` | 144 行 | `MessageEvent`、`MessageType`：所有平台统一的入站消息结构 |
| `gateway/platforms/base.py` | 4933 行 | `BasePlatformAdapter`：连接生命周期、会话锁/打断/排队、文本合并、媒体提取、TTS、投递账本、发送重试与纯文本降级、`truncate_message` 分段 |
| `gateway/platforms/weixin.py` | 单文件 | 微信适配器 |
| `gateway/platforms/qqbot/` | `adapter.py` 1674 行 + 6 个辅助文件 | QQ 适配器（含键盘、分片上传、语音识别等） |
| `gateway/run.py` + 数十个 `run_*.py` | — | `GatewayRunner`：把事件交给 Agent、会话管理、cron 投递 |

**要保留的核心契约只有一条**：适配器把平台消息转成 `MessageEvent` 交给 handler；对外提供 `send(chat_id, text)`。其余（会话锁、打断、媒体、TTS、账本、多 profile）都属于 hermes 自身的 Agent 运行模型，本项目由 dispatcher 负责，不移植。

### 3.2 微信：腾讯 iLink Bot API（官方 ClawBot 通道）

hermes 的微信不是 hook / 网页版协议，而是腾讯官方的 iLink Bot 接口：扫码后得到一个 bot 身份（`xxx@im.bot`），用户在微信里和这个 bot 私聊。

- Base URL：`https://ilinkai.weixin.qq.com`；所有 POST body 追加 `base_info: {channel_version: "2.2.0"}`。
- 请求头：`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN: base64(随机 uint32 的十进制字符串)`、`iLink-App-Id: bot`、`iLink-App-ClientVersion: 131584`。

| 用途 | 接口 | 要点 |
| --- | --- | --- |
| 获取登录二维码 | `GET ilink/bot/get_bot_qrcode?bot_type=3` | 返回 `qrcode`、`qrcode_img_content`（扫码要用后者这个 URL） |
| 轮询扫码状态 | `GET ilink/bot/get_qrcode_status?qrcode=` | `wait` / `scaned` / `scaned_but_redirect`（切换到 `redirect_host`）/ `expired`（最多刷新 3 次）/ `confirmed` → 返回 `ilink_bot_id`、`bot_token`、`baseurl`、`ilink_user_id` |
| 收消息 | `POST ilink/bot/getupdates` | 长轮询约 35s；请求带游标 `get_updates_buf`，响应返回新游标，**必须持久化**；`longpolling_timeout_ms` 可动态调整超时 |
| 发消息 | `POST ilink/bot/sendmessage` | `msg = {to_user_id, client_id, message_type: 2, message_state: 2, item_list: [{type: 1, text_item: {text}}], context_token}` |
| 输入状态 | `getconfig` + `sendtyping` | 可选，不移植 |
| 图片 / 文件 | `getuploadurl` + CDN（AES-128-ECB） | 不移植 |

关键机制：

1. **context_token**：每条入站消息都带，回复时必须回传该用户最新的 token；hermes 按 (账号, 用户) 持久化。
2. **错误码**：`-14` 表示会话过期（收消息时暂停 10 分钟；发消息时去掉 context_token 重发一次）；`-2` 且 errmsg 为 `unknown error` / `prepare failed` 表示**会话未就绪，必须等用户先给 bot 发一条消息**；其他 `-2` 是频率限制（3 倍退避，并熔断 30s）。
3. **去重**：按 `message_id`，再加一层“发送者 + 内容 md5”（iLink 会用新 id 重发相同内容），TTL 300s。
4. **只支持私聊**：iLink bot 身份通常无法拉进普通群。
5. **发送节奏**：单条上限 2000 字，按约 1800 字切分；分段间隔 1.5s；单段最多重试 4 次。
6. **稳定性**：连续失败 3 次后退避 30s 并重建 HTTP 会话（防止代理环境下 socket 泄漏）。

### 3.3 QQ：QQ 机器人官方 API v2

| 用途 | 接口 | 要点 |
| --- | --- | --- |
| 获取 token | `POST https://bots.qq.com/app/getAppAccessToken`，body `{appId, clientSecret}` | 返回 `access_token`、`expires_in`（约 7200s）；提前 60s 刷新，加锁防并发刷新 |
| 网关地址 | `GET https://api.sgroup.qq.com/gateway` | 请求头 `Authorization: QQBot <token>` |
| 收消息 | WebSocket | 见下方流程 |
| 发私聊 | `POST /v2/users/{openid}/messages` | `{content, msg_type: 0, msg_seq}`，markdown 用 `{markdown: {content}, msg_type: 2}`；带 `msg_id` 即被动回复 |
| 发群 | `POST /v2/groups/{group_openid}/messages` | 同上 |

WebSocket 流程：

1. 收到 `op10 Hello`（含 `heartbeat_interval`）→ 有 session 时发 `op6 Resume {token, session_id, seq}`，否则发 `op2 Identify {token: "QQBot <token>", intents, shard: [0, 1]}`。
2. `op0 READY` 保存 `session_id`；之后每条 `op0` 都更新 `last_seq`。
3. 按心跳间隔的 80% 发送 `op1 {d: last_seq}`；`op7` 表示服务端要求重连；`op9` 表示会话失效。
4. 入站事件：`C2C_MESSAGE_CREATE`（`author.user_openid`）、`GROUP_AT_MESSAGE_CREATE`（`group_openid`、`author.member_openid`，需去掉开头的 @）。
5. 关闭码：`4004` token 失效，刷新后重连；`4006` / `4007` / `4900–4913` 清除 session 重新 Identify；`4008` 限流等待；`4001` / `4002` / `4010–4014` / `4914` / `4915` 为致命错误（权限、沙箱、封禁），停止重连；短时间反复断开超过阈值视为配置错误。
6. `msg_seq` 取 0–65535 的随机数，对同一个 `msg_id` 多次回复必须用不同的 seq。

hermes 订阅了 `(1<<25)|(1<<30)|(1<<12)|(1<<26)`（单聊群聊、频道、频道私信、按钮交互）。**本项目只订阅 `1<<25`**，减少因权限不足触发 `4014` 的概率。

### 3.4 对“定时推送”场景的影响（重要）

两个平台都限制 bot 主动发消息：

- **微信**：主动推送依赖用户最近一次消息带来的 context_token。token 过期时 hermes 会去掉 token 降级重发；如果返回“会话未就绪”，就只能等用户再发一条消息。
- **QQ**：不带 `msg_id` 的是主动消息，平台对主动消息有额度或权限限制；被动回复依赖 `msg_id`，且有时效。具体规则以 QQ 开放平台当期文档为准，T2.3 的 live 测试中确认。

结论：21:00 推送**不能假设一定送达**，`router` 必须实现**补投递**：

1. 发送失败且原因是“会话未就绪 / 主动消息受限”时，`delivery.status` 记为 `waiting_user`，不再盲目重试。
2. 该用户下次发来任何消息时，先把积压的报告作为被动回复补发，再处理这条新消息。
3. 支持同一订阅配置微信、QQ 两个通道：主通道失败自动改走另一个。
4. 管理界面展示 `waiting_user` 状态和积压数量。

## 4. 移植方案

### 4.1 取舍

| 目标文件 | 来源 | 保留 | 删除 | 预计行数 |
| --- | --- | --- | --- | --- |
| `gateway/base.py` | `event.py`、`base.py`、`helpers.py` | 精简版 `MessageEvent` / `SendResult`；`BaseAdapter` 抽象方法；`truncate_message` 的代码块感知分段；`MessageDeduplicator`；可重试错误判断 | 会话锁/打断/排队、文本合并去抖、媒体提取与缓存、TTS、投递账本、profile 路由、审批按钮、插件、i18n | ~250 |
| `gateway/weixin.py` | `weixin.py` | 请求头与 `_api_post` / `_api_get`、`qr_login`、`getupdates` 长轮询 + 游标持久化、`ContextTokenStore`、文本提取（含引用）、去重、分段发送 + 重试 + 限流熔断 + 会话过期降级 | 媒体 AES 上传下载、输入状态、群策略、文本批量合并、平台锁、`send_weixin_direct` / `_LIVE_ADAPTERS`、markdown 换行美化 | ~450 |
| `gateway/qqbot.py` | `qqbot/adapter.py`、`constants.py`、`utils.py` | token 刷新、网关 WebSocket、心跳、Identify / Resume、关闭码处理与重连退避、单聊 / 群 @ 收消息、文本 / markdown 发送 + 重试 | 键盘与交互审批、分片上传、语音识别、附件、频道（guild）消息、onboard、crypto | ~500 |
| `gateway/auth.py` | `pairing.py`、`access_policy_mixin.py` 的思路 | 白名单 + 配对码 | 多策略组合、群策略 | ~120 |
| `gateway/router.py` | `delivery.py` 的思路（重写） | 适配器注册、入站分发、出站分段发送、`delivery` 记录、补投递 | — | ~250 |

### 4.2 适配器契约（`gateway/base.py`）

```python
@dataclass
class MessageEvent:
    platform: str            # "weixin" | "qqbot"
    chat_id: str             # 微信 from_user_id；QQ user_openid 或 group_openid
    chat_type: str           # "dm" | "group"
    user_id: str
    text: str
    message_id: str | None
    timestamp: datetime
    raw: dict

@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    need_user: bool = False  # 会话未就绪 / 主动消息受限，触发补投递

class BaseAdapter(ABC):
    platform: str
    max_length: int
    def set_handler(self, handler: Callable[[MessageEvent], Awaitable[None]]) -> None: ...
    @abstractmethod
    async def connect(self) -> bool: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def send(self, chat_id: str, text: str, reply_to: str | None = None) -> SendResult: ...

def split_text(text: str, limit: int) -> list[str]: ...  # 移植自 truncate_message
```

### 4.3 改造要点

1. **HTTP 统一用 httpx**：hermes 的微信用 aiohttp，QQ 混用 aiohttp 和 httpx。本项目 REST 统一用 httpx（测试可直接用 respx mock），QQ WebSocket 用 `websockets` 库。只需改写 `_api_request` 和建立 WebSocket 连接的函数。
2. **配置**：hermes 的 `config.extra` 和 `WEIXIN_*` / `QQ_*` 环境变量，改为 `core.config.settings.weixin` / `settings.qq`；环境变量名保持一致，方便对照。
3. **持久化**：hermes 写 `~/.hermes/weixin/accounts/*.json`，改为写 `data/weixin/`（账号凭证、同步游标、context_token），文件权限 0600，不入库。
4. **鉴权集中**：hermes 每个适配器自带 `dm_policy` / `allow_from`；本项目适配器只产出事件，鉴权统一由 `router` 调用 `auth.py`。
5. **去掉会话锁**：适配器回调直接返回，长任务交给 dispatcher 队列；“任务执行中又来新消息”由 dispatcher 决定排队还是取消。
6. **隐私**：保留 hermes `_safe_id` 截断用户 id 后再写日志的做法。
7. **登录命令**：`python -m newsbot login weixin` 调用 `qr_login` 在终端打印二维码。

新增依赖：`httpx`、`websockets`、`qrcode`（终端二维码，可选）。去掉媒体后不再需要 `cryptography`。

## 5. 移植步骤（T2.1–T2.4 执行）

1. 在仓库外克隆 hermes-agent 并切到锁定 commit，不要把整个仓库放进本项目。
2. 按第 4.1 节逐文件拷贝所需代码，加模块头注释（含 `来源:`），删除不保留的分支逻辑。
3. 按第 4.3 节替换基础设施，去掉对 hermes 内部模块的 import。
4. 补齐单元测试：respx mock iLink / QQ REST，伪造 WebSocket 服务端验证 Hello→Identify→READY→心跳→断线 Resume；覆盖第 3.2、3.3 节列出的每个错误码分支。
5. 编写 `live` 测试（需真实凭证，手动运行）。
6. 在 `THIRD_PARTY_NOTICES.md` 登记，并更新第 7 节移植记录。

## 6. 风险提示

- **微信**：iLink 是腾讯官方 bot 通道，比个人号 hook 安全，但仍受频率限制和“会话未就绪”约束（见 3.4）。若不可用，备选企业微信应用消息 / 群机器人 Webhook（仅推送）。
- **QQ**：需在 QQ 开放平台申请 AppID / Secret，开通单聊 / 群聊权限并配置沙箱或通过审核，属于人工步骤。
- 平台凭证申请、扫码登录属于人工任务，长跑开发遇到时按 [07-autonomous-dev.md](07-autonomous-dev.md) 第 4 节停止并汇报。

## 6.1 browser-use 集成实录（T11.1）

browser-use 是成熟内核，但**默认配置在我们的环境里跑不起来**，以下每一条都对应一次实测失败。

| 项 | 默认行为 | 问题 | 我们的做法 |
| --- | --- | --- | --- |
| `llm` | `None` | 它是 LLM 驱动的 Agent，不给模型无法动作 | 用 `ChatOpenAI(base_url=…)` 接我们的 OpenAI 兼容端点 |
| 模型选择 | 随便给一个 | `deepseek-v4-flash` 单次调用 17s 且 JSON 常被截断/损坏，90s 超时频繁触发 | 单独配置 `LLM_MODEL_BROWSER`，实测 `deepseek-v4-1-flash-260910` 约 2s 且 JSON 合法 |
| `enable_default_extensions` | `True` | 启动时联网下载 uBlock 等扩展，受限网络下 CRX 无效并把启动拖过 30s | 关掉 |
| `use_vision` | `True` | 非视觉模型收到截图会报错，且白烧 token | 显式 `False` |
| 浏览器会话 | 每次新建 | 启动约 4s，重复任务纯浪费 | 复用同一个 `BrowserSession`，`aclose()` 收尾 |
| `TIMEOUT_BrowserStartEvent` | 30s | 冷启动或被残留进程占用时莫名失败，错误信息只有一串事件名 | 用环境变量对齐到 `agent.browser_timeout_seconds` |
| 运行产物 | `~/.cache`、`~/.config` | 违反“运行时产物只写 `data/`” | 改 `XDG_CACHE_HOME`、`XDG_CONFIG_HOME`、`BROWSER_USE_CONFIG_DIR` 指向 `data/browser/` |
| 匿名遥测 | 开启 | 受限网络里是纯粹拖慢 | `ANONYMIZED_TELEMETRY=false` |
| 输出契约 | 无 | **会编造未被要求的字段**（详见下） | `TASK_SUFFIX` 逐条禁止 |

### 为什么必须加输出契约

在 `https://github.com/trending` 上实测：该页面**只公布「今日新增 star」，没有总 star 与 fork**
（已核对原始 HTML：含 fork 的文本 0 处）。但子 Agent 主动补充了「总 star 38,744」「fork 5,834」
等 6 个数字，其中 4 个在页面上完全不存在——而同一段输出还自称“未添加页面以外的信息”。

收紧 `TASK_SUFFIX`（只报被要求的字段、数字必须逐字照抄、缺失写「页面未提供」、不许自我评价）
之后，输出只剩被要求的 3 个数字，且**全部可在页面原文中回查**。
`tests/live/test_live_browser.py` 把这个判据固化成断言：输出里的每个数字都必须出现在页面原文里。

### 已知限制

- 残留的浏览器进程会占住 profile 目录，导致后续启动被判超时。因此 `App.stop()` 必须走到
  `runner.aclose()`（见 `fix(app): 关闭服务时回收 Agent 工具`）；调试时若强杀进程，需按命令行
  过滤清理 `chrome.exe`，切勿误杀用户自己的浏览器。
- 复用 `data/browser/profile` 是为将来复用登录态（如需要登录的站点）留的位置，目前尚未做登录。

## 7. 移植记录

| 源文件 | 上游 commit | 目标文件 | 协议 / 接入方式 | 新增依赖 | 删改要点 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `gateway/platforms/event.py`、`base.py`、`helpers.py` | main@908e4a4 | `gateway/base.py` | — | — | 见 4.1 | 已移植 |
| `gateway/platforms/weixin.py` | main@908e4a4 | `gateway/weixin.py` | 腾讯 iLink Bot API，HTTP 长轮询 | httpx | 见 4.1 | 已移植 |
| `gateway/platforms/qqbot/adapter.py`、`constants.py` | main@908e4a4 | `gateway/qqbot.py` | QQ Bot API v2，WebSocket + REST | httpx、websockets | 见 4.1 | 已移植 |
| `gateway/pairing.py`、`platforms/access_policy_mixin.py` | main@908e4a4 | `gateway/auth.py` | — | — | 仅参考思路 | 已编写 |
| `gateway/delivery.py` | main@908e4a4 | `gateway/router.py` | — | — | 重写，新增 waiting_user 补投递 | 已编写 |
| `browser-use/browser-use` | 0.11.13（pip） | `agent/tools/browser.py` | pip 依赖，不拷贝源码 | browser-use（可选 extra） | 见 6.1：会话复用、跳过扩展下载、输出契约 | 已接入 |
