from __future__ import annotations
from pathlib import Path
from typing import Any
from typing import Literal
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Schedule(BaseModel):
    time: str = "08:00"
    coverage: Literal["yesterday"] = "yesterday"
    timezone: str = "Asia/Shanghai"


class HotLeads(BaseModel):
    retention_days: int = 14


class LLM(BaseModel):
    endpoint: str
    model: str
    api_key_env: str
    max_tokens: int = 16000
    timeout: float = 300.0
    extra_body: dict[str, Any] = Field(default_factory=dict)


class Telegram(BaseModel):
    bot_token_env: str
    chat_id_env: str


class WechatSource(BaseModel):
    groups: list[str] = Field(default_factory=list)


class TelegramChat(BaseModel):
    id: str
    name: str
    limit: int = 500


class TelegramSource(BaseModel):
    enabled: bool = False
    db_path: str = "~/Library/Application Support/tg-cli/messages.db"
    chats: list[TelegramChat] = Field(default_factory=list)
    sync_before_export: bool = True


class Sources(BaseModel):
    wechat: WechatSource = Field(default_factory=WechatSource)
    telegram: TelegramSource = Field(default_factory=TelegramSource)


class Retry(BaseModel):
    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [5, 15, 60])


class Sanitize(BaseModel):
    enabled: bool = False


class Config(BaseModel):
    groups: list[str] | None = None
    sources: Sources = Field(default_factory=Sources)
    todo: list[str] = Field(default_factory=list)
    schedule: Schedule = Field(default_factory=Schedule)
    hot_leads: HotLeads = Field(default_factory=HotLeads)
    llm: LLM
    telegram: Telegram
    retry: Retry = Field(default_factory=Retry)
    sanitize: Sanitize = Field(default_factory=Sanitize)

    @field_validator("groups")
    @classmethod
    def groups_nonempty(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not v:
            raise ValueError("groups must contain at least one group name")
        return v

    @model_validator(mode="after")
    def normalize_sources(self) -> "Config":
        if self.groups is not None and not self.sources.wechat.groups:
            self.sources.wechat.groups = self.groups
        self.groups = self.sources.wechat.groups

        has_wechat = bool(self.sources.wechat.groups)
        has_telegram = self.sources.telegram.enabled and bool(self.sources.telegram.chats)
        if not has_wechat and not has_telegram:
            raise ValueError("configure at least one source: sources.wechat.groups or sources.telegram.chats")
        return self


def load_config(path: Path) -> Config:
    from chat_daily_tg.paths import migrate_legacy_config_if_needed

    migrate_legacy_config_if_needed(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)
