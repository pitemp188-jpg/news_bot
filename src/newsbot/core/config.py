"""
模块: core.config
职责: 加载配置——密钥读 .env，非敏感项读 config.yaml，提供必填校验与数据库地址
依赖: core.errors
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from newsbot.core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[3]


class Secrets(BaseSettings):
    """敏感配置，来自 .env 或环境变量（环境变量优先）。"""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    search_provider: str = "searxng"
    searxng_url: str = "http://127.0.0.1:8080"
    tavily_api_key: str = ""

    weixin_token: str = ""
    weixin_account_id: str = ""
    weixin_base_url: str = "https://ilinkai.weixin.qq.com"
    weixin_allowed_users: str = ""

    qq_app_id: str = ""
    qq_client_secret: str = ""
    qq_allowed_users: str = ""

    admin_password: str = ""


class AppSection(BaseModel):
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"


class QueueSection(BaseModel):
    concurrency: int = 2
    task_timeout_seconds: int = 600
    max_attempts: int = 2
    retry_delay_seconds: float = 5.0


class ScheduleSection(BaseModel):
    """定时推送与去重配置。去重配置放在这里是因为定时推送正是去重的主要场景。"""

    default_time: str = "21:00"
    default_topics: list[str] = Field(default_factory=lambda: ["AI"])
    dedup_days: int = 7
    # 语义去重：让模型判断"这几条是不是同一件事"，补上词法判据在中文改写、
    # 无版本号的纯中文事件上的盲区。模型只做分组，保留哪一条仍由确定性规则决定；
    # 关掉就只用词法判据（零模型成本）
    dedup_with_llm: bool = True
    # 语义分组的超时。默认 90s：当前模型是推理模型，一次分组实测 12～40s，而模型
    # 客户端自身还会重试 3 次（间隔 2/4/8s），设得太小会在网络抖动时白白放弃分组
    dedup_llm_timeout_seconds: float = 90.0


class AgentSection(BaseModel):
    # 规划循环与预算。max_tokens 是**累计**的（每步都把 prompt+completion 累加），
    # 循环里 prompt 会随对话增长，所以 50 步大体对应 100 万 token 的量级
    max_steps: int = 50
    max_tokens: int = 1_000_000
    fetch_max_bytes: int = 2_000_000
    search_results: int = 8
    browser_concurrency: int = 1
    # 浏览器动作的单次时限；浏览器侧不再调用 LLM，耗时由页面加载决定
    browser_timeout_seconds: int = 120
    # 服务端默认无头；本地调试想看到窗口时改为 false
    browser_headless: bool = True


class DeliverySection(BaseModel):
    chunk_delay_seconds: float = 1.5
    max_attempts: int = 3
    fallback_platform: str = "qqbot"


class AutomationSection(BaseModel):
    """事件层的自动化：定时采集（进料）与新事件告警（主动推送）。

    为什么与 `EditorialSection` 分开：那边是"怎么选、怎么写"的口径参数，这里是
    "多久动一次、往哪推"的节奏与投递参数。混在一起会让调精选门槛的人顺手改到推送频率。
    """

    # ── 采集：让热榜的进料不再依赖人问一句 ──
    # 关掉就退回"只有提问与订阅才会进料"的行为（库里会重新变得稀疏）
    collect_enabled: bool = True
    # 采集间隔。30 分钟与事件层刷新同频：实测 15 个源一次拉取约 5～15s，
    # 半小时一次的代价可以忽略，而更疏会让"新出现"的热点晚半天才被发现
    collect_interval_minutes: int = 30
    # 时间窗：只要最近这么多天的条目。订阅源本身是内容流，但实测有的源夹带几个月前的旧文
    collect_days: int = 1
    # 单次**取回**条数上限（去重前）。它是取回上限而不是入库上限：采集每半小时一次，
    # 同一批条目会被反复取回，实测一轮取回 120 条只有 34 条是新的。设小了会漏掉
    # "排在后面的新条目"（排序按发布时间倒序，丢的是最旧的），所以给得比预期入库量大
    collect_max_items: int = 300
    # 采集时单个源的上限。比检索的 8s 宽得多：实测 15 个源里有 5 个在 8s 内返回不了，
    # 用检索的上限去采集等于稳定漏掉三分之一的内容（背景作业多等十几秒没人察觉）
    collect_feed_timeout_seconds: float = 25.0

    # ── 告警：发现够格的新事件就推给你 ──
    alert_enabled: bool = True
    # 推送目标。留空则回退到"最近一次成功投递过的会话"——那通常就是你在用的那个群
    alert_platform: str = "qqbot"
    alert_chat_id: str = ""
    # 单次最多推几条。这是防刷屏的硬护栏：热搜同时爆发时宁可漏推，
    # 也不要把一次刷新变成一串消息（漏掉的下一轮还会在榜上，不会消失）
    alert_max_per_run: int = 3
    # 新事件的判定窗口与热度榜的"趋势"口径一致（`hot.TREND_GAP_HOURS` 小时前热度为 0），
    # 这里只放"上线首轮不推历史积压"的护栏：库里从没推过任何事件时，
    # 先把当前达标的全部标记为已推，避免第一次运行就把几十个旧事件一次推光
    alert_bootstrap: bool = True


class EditorialSection(BaseModel):
    """精选（日报/事件页的入选判定）配置。

    门槛按**信源分级**而不是一刀切：官方一手发布（T1）本身就值得进日报，媒体转载
    （T2）得多条互相印证。分层的意义在于"同一份标准下，不同来源的入选代价不同"，
    而不是给官方开后门——官方来源一样要过同一份打分标准，只是门槛更低。
    """

    # 分级 → 入选门槛（0～10 分）。级别越高（越远离一手）门槛越高
    tier_thresholds: dict[str, int] = Field(default_factory=lambda: {"T1": 6, "T1_5": 7, "T2": 8})
    # 官方一手来源域名（进 T1）；官方账号 / 准官方（进 T1_5）。留空表示全部按 T2
    official_domains: list[str] = Field(default_factory=list)
    quasi_official_domains: list[str] = Field(default_factory=list)
    # 社区来源域名：讨论确实代表热度，贡献参与者计数，但**不计入独立来源数**
    # （无法核对内容是否属实）。命中的条目 `editorial=False`
    community_domains: list[str] = Field(default_factory=lambda: ["news.ycombinator.com", "lobste.rs", "reddit.com"])
    # 单次打分调用的超时。当前模型是推理模型，一次 0～10 分实测数秒到数十秒
    score_timeout_seconds: float = 60.0
    # 推理 token 与正文共用额度，给太小会出现"调用成功但正文为空"，需留足余量
    score_max_tokens: int = 4000
    # |score1 - score2| 达到这个值就标为"标准在它身上不稳定"，需人工复核
    unstable_gap: int = 3
    # 一次精选最多打分多少条：逐条两次调用 = 2 倍成本，条数必须有上限
    max_candidates: int = 40
    # 聚簇只看最近这么多天的资讯。两周：再早的报道与今天的内容几乎不可能同属一件事，
    # 放进候选只是把模型输入撑大
    cluster_days: int = 14
    # 候选预筛的词集相似度下限。实测依据（158 条真实资讯两两比对，12403 对）：
    #   0.08 → 215 对，0.12 → 57，0.15 → 28，0.20 → 13
    # 而 0.12～0.25 这一段的配对**逐条看过，全是噪声**（「Best Battery Leaf Blowers」对
    # 「Power Supply Solutions」、「Elon Musk 攻击 Ambani」对「Musk 的中期选举开支」）。
    # 0.08 等于把九成模型调用烧在噪声上——实测 19 批判定里 5 批返回空正文、其余清一色
    # different。取 0.15：调用量降到约四分之一，同时留出召回余量。**最终判定仍是模型**，
    # 多问几对的代价只是 token，而漏问会让同一件事裂成两条各自进日报。
    cluster_pair_floor: float = 0.15
    # 一次让模型看多少对配对。**这是"每次调用"的批量，不是总量上限**：实测一批给
    # 60 对会让推理把输出额度吃光、返回空正文（63 秒后拿到空字符串）。分批后每批
    # 推理量变小，正文能正常产出，而且全部配对都判得到——不会像截断那样静默丢掉大部分
    cluster_max_pairs: int = 8
    # 配对判定的输出上限。**必须给足**：这个端点是推理模型，推理与正文共用额度，
    # 实测 12 对配对就让推理烧掉整整 4000 token（日志 completion=4000、正文为空）。
    # 上限给小了的后果不是"答案短"而是"完全没有答案"；它只是上限，不会白花钱
    cluster_max_tokens: int = 16000
    # 配对判定的单批超时。**必须跟着输出上限一起调**：上限提到 16000 后模型有机会
    # 真的写完推理，单批实测从数十秒涨到两分钟以上——实测 120 秒会让整批超时丢结论，
    # 而"丢结论"会连锁触发"先合并、再被复核拆开"的无谓往返
    cluster_timeout_seconds: float = 240.0
    # 配对总数上限（成本兜底）。超出的部分**按预筛相似度从高到低**保留：
    # 最像的配对优先送进模型，而不是按编号顺序碰运气
    cluster_pair_budget: int = 300
    # 事件层刷新周期（分钟）。聚簇与两轮打分都是模型调用，成本随资讯量增长，
    # 0 表示关闭自动刷新（只保留手动 `python -m newsbot events`）
    refresh_interval_minutes: int = 30
    # 一次刷新最多给多少条报道打分。逐条两次调用 = 2 倍成本，条数必须有上限
    refresh_max_scores: int = 24
    # 一次刷新最多为多少个事件写综述。只在**多来源**事件且上一位综述已过期时才写，
    # 所以常态下这个上限很少触顶；触顶时优先处理"新报道攒得最多"的那些
    refresh_max_summaries: int = 12
    # 综述的输出上限。**必须给足**：这个端点是推理模型，推理与正文共用额度，
    # 上限给小了的后果不是"写得短"而是"完全没有内容"——实测同一个端点在上限 4000
    # 时返回过空正文（聚簇那边已因此踩过一次）。它只是上限，不会白花钱
    summary_max_tokens: int = 8000
    # 综述的单条超时。跟着输出上限走：上限提到 8000 后模型有机会真的写完推理
    summary_timeout_seconds: float = 150.0

    def tier_of(self, source: str, url: str) -> str:
        """按来源域名判信源分级；不在名单里一律 T2。

        用"是否在名单里"而不是猜测：把自己不认识的来源判成官方，等于替读者做了
        "这条更可信"的背书，而背书必须有据可依。
        """
        host = _host_of(source) or _host_of(url)
        if not host:
            return "T2"
        if _matches(host, self.official_domains):
            return "T1"
        if _matches(host, self.quasi_official_domains):
            return "T1_5"
        return "T2"

    def is_community(self, url: str) -> bool:
        """是否社区来源：讨论算热度，但不计入独立来源数。"""
        return _matches(_host_of(url), self.community_domains)


class FeedSource(BaseModel):
    """一个订阅源：地址、质量权重与行业标签。

    权重不是装饰：同一件事被多家媒体报道时，去重要靠它挑出**保留**哪一条；
    检索排序也靠它把权威源顶到前面。默认 1.0，官方源 / 一线媒体给 1.0～1.2，
    聚合与消费级媒体给 0.7～0.9。
    """

    url: str
    weight: float = 1.0
    topics: list[str] = Field(default_factory=list)


def _host_of(url: str) -> str:
    """取出主机名。同时接受网址与纯域名（`openai.com`），都去掉 `www.`。

    来源字段里两种写法都实测出现过：订阅源给域名，搜索结果给完整网址。
    """
    text = (url or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0].split("@")[-1].split(":", 1)[0].removeprefix("www.")


def _matches(host: str, domains: list[str]) -> bool:
    """主机名是否命中域名名单；子域名算命中，**不做子串匹配**。

    `notopenai.com` 不能因为包含 `openai.com` 就被当成官方——域名边界必须显式判断。
    """
    if not host:
        return False
    for raw in domains:
        domain = _host_of(raw)
        if domain and (host == domain or host.endswith(f".{domain}")):
            return True
    return False


def default_feeds() -> list[FeedSource]:
    """按行业分组的默认订阅源。

    每个源都做过实测（可达性、解析条数、是否给真实文章地址、发布时间）。被排除的
    源连同实测结论留在这里，避免以后有人凭印象加回来：

    - `36kr.com/feed`：返回的是验证拦截页（HTML），解析出 0 条
    - `jiqizhixin.com/rss`、`anandtech.com/rss`、`hashnode.com/rss`：HTTP 200 但 0 条
    - `github.blog/feed`、`aws.amazon.com/blogs/aws/feed`、`devblogs.microsoft.com/feed`、
      `feeds.arstechnica.com`、`engadget.com/rss.xml`、`the-decoder.com/feed`、
      `krebsonsecurity.com/feed`、`feeds.feedburner.com/TheHackersNews`：连接超时
    - `bleepingcomputer.com/feed`：403
    - `reddit.com/r/MachineLearning/.rss`：连接错误

    还有几个源在本机网络下**时通时断**（`venturebeat.com`、`lobste.rs`、`theverge.com`、
    `hnrss.org`）：实测同一源有时 0.4s 返回、有时 8s 超时。它们仍保留在列表里，因为
    单源超时会被跳过、不影响整次检索；但也不该是唯一来源。
    """
    return [
        # ── AI ──
        # 量子位：中文 AI 一线，实测 0.5s / 10 条，摘要偶尔为空（正常，正文交给 fetch）
        FeedSource(url="https://www.qbitai.com/feed", weight=1.2, topics=["AI"]),
        # VentureBeat：英文 AI 产业报道，实测 0.5～2.8s / 7 条
        FeedSource(url="https://venturebeat.com/feed/", weight=1.0, topics=["AI"]),
        # MarkTechPost：覆盖广但对发布方自述照抄较多，权重下调
        FeedSource(url="https://www.marktechpost.com/feed/", weight=0.9, topics=["AI"]),
        # ── 开发 / 云原生 ──
        # InfoQ 中文：工程实践质量高，实测 0.4s / 20 条（摘要多为空，是站点本身如此）
        FeedSource(url="https://www.infoq.cn/feed", weight=1.1, topics=["开发"]),
        # Kubernetes 官方博客：一手信息，实测 2.9s / 50 条
        FeedSource(url="https://kubernetes.io/feed.xml", weight=1.0, topics=["开发", "云原生"]),
        # ── 综合科技 ──
        # 爱范儿：中文消费科技，实测 0.5s / 20 条
        FeedSource(url="https://www.ifanr.com/feed", weight=1.0, topics=["科技"]),
        FeedSource(url="https://techcrunch.com/feed/", weight=1.0, topics=["科技", "创业"]),
        # Solidot：中文科技短讯，摘要完整
        FeedSource(url="https://www.solidot.org/index.rss", weight=0.9, topics=["科技"]),
        FeedSource(url="https://www.wired.com/feed/rss", weight=0.9, topics=["科技"]),
        FeedSource(url="https://www.theverge.com/rss/index.xml", weight=0.9, topics=["科技"]),
        # ── 芯片 / 硬件 ──
        # 半导体工程：面向设计与工艺的一手报道，实测 4.6s / 10 条
        FeedSource(url="https://semiengineering.com/feed/", weight=1.0, topics=["芯片"]),
        FeedSource(url="https://www.eetimes.com/feed/", weight=1.0, topics=["芯片"]),
        # Tom's Hardware：覆盖消费级硬件，权威度低于前两者，实测 2.2～7.2s / 50 条
        FeedSource(url="https://www.tomshardware.com/feeds/all", weight=0.7, topics=["硬件"]),
        # ── 社区 ──
        # Lobsters：技术社区精选，实测 1.0～4.5s / 25 条
        FeedSource(url="https://lobste.rs/rss", weight=0.8, topics=["开发"]),
        # Hacker News：实测时通时断（成功 1.9s，失败 5s 超时）；单源超时会跳过它，
        # 保留是因为内容价值高，但它不应是唯一来源
        FeedSource(url="https://hnrss.org/frontpage", weight=0.7, topics=["开发", "科技"]),
    ]


class SearchSection(BaseModel):
    """搜索配置。`feeds` 用于 SEARCH_PROVIDER=feeds：订阅权威源的 RSS，按关键词过滤。"""

    # 只放**实测可直连、能解析出真实文章地址**的源；加源前先跑一次实测，
    # 否则一个连不上的源只会白等一次超时（下面每条都标了实测结论）
    feeds: list[FeedSource] = Field(default_factory=default_feeds)
    # 单次拉取的并发上限，避免一次打开太多连接。实测 6 并发在连续检索时会让多个源
    # 同时失败（网络侧限速），4 稳定
    feed_concurrency: int = 4

    @field_validator("feeds", mode="before")
    @classmethod
    def _accept_plain_urls(cls, value: Any) -> Any:
        """允许 config.yaml 里只写网址：`feeds: ["https://a/feed"]` 等价于 weight=1、无行业标签。"""
        if not isinstance(value, list):
            return value
        return [{"url": item} if isinstance(item, str) else item for item in value]

    def feed_weights(self) -> dict[str, float]:
        """域名 → 权重；搜索工具用它给命中项加权，让权威源排在前面。"""
        return {_host_of(feed.url): feed.weight for feed in self.feeds}


class ApiSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Config(BaseModel):
    """合并 config.yaml 与 .env 后的运行配置。"""

    model_config = ConfigDict(extra="ignore")

    app: AppSection = Field(default_factory=AppSection)
    queue: QueueSection = Field(default_factory=QueueSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    agent: AgentSection = Field(default_factory=AgentSection)
    search: SearchSection = Field(default_factory=SearchSection)
    delivery: DeliverySection = Field(default_factory=DeliverySection)
    automation: AutomationSection = Field(default_factory=AutomationSection)
    editorial: EditorialSection = Field(default_factory=EditorialSection)
    api: ApiSection = Field(default_factory=ApiSection)
    secrets: Secrets = Field(default_factory=Secrets)
    data_dir: Path = ROOT / "data"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'newsbot.db'}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_ready(self) -> None:
        """启动前校验必填项；缺失直接抛错，避免运行到一半才失败。"""
        missing = []
        if not self.secrets.llm_api_key:
            missing.append("LLM_API_KEY")
        if self.secrets.search_provider == "tavily" and not self.secrets.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        if missing:
            raise ConfigError("缺少必填配置: " + "、".join(missing) + "（请写入 .env）")


def load_config(config_file: Path | None = None) -> Config:
    """读取配置文件与密钥；配置文件不存在时全部使用默认值。"""
    path = config_file or ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"配置文件应为映射结构: {path}")
        raw = loaded or {}

    data_dir = os.environ.get("NEWSBOT_DATA_DIR") or raw.pop("data_dir", None) or ROOT / "data"
    return Config(**raw, data_dir=Path(data_dir))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """进程内单例配置。"""
    return load_config()
