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
    return url.split("//")[-1].split("/")[0].removeprefix("www.")


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
