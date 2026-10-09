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
from pydantic import BaseModel, ConfigDict, Field
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
    default_time: str = "21:00"
    default_topics: list[str] = Field(default_factory=lambda: ["AI"])
    dedup_days: int = 7


class AgentSection(BaseModel):
    max_steps: int = 12
    max_tokens: int = 60000
    fetch_max_bytes: int = 2_000_000
    search_results: int = 8
    browser_concurrency: int = 1


class DeliverySection(BaseModel):
    chunk_delay_seconds: float = 1.5
    max_attempts: int = 3
    fallback_platform: str = "qqbot"


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
