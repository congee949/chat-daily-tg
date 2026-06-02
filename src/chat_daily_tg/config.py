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


class OptionalModel(LLM):
    enabled: bool = False


class EmbeddingModel(OptionalModel):
    dimension: int = 768
    top_k: int = 8
    min_similarity: float = 0.35


class ImageModel(OptionalModel):
    mode: Literal["off", "auto", "always"] = "off"


class Models(BaseModel):
    summary: LLM
    vision: OptionalModel | None = None
    image: ImageModel | None = None
    embedding: EmbeddingModel | None = None


class Telegram(BaseModel):
    bot_token_env: str
    chat_id_env: str
    send_image: bool = False  # render the daily summary as a PNG card and sendPhoto before the text
    image_only: bool = False  # if send_image and the photo sends OK, skip the text message
                              # (text is still sent as fallback when rendering/sendPhoto fails)
    image_caption: bool = True  # attach a short text caption to the photo; False = pure image


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
    llm: LLM | None = None
    gemini: LLM | None = None
    models: Models | None = None
    telegram: Telegram
    retry: Retry = Field(default_factory=Retry)
    sanitize: Sanitize = Field(default_factory=Sanitize)
    source_abbreviations: dict[str, str] = Field(default_factory=dict)

    @field_validator("groups")
    @classmethod
    def groups_nonempty(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not v:
            raise ValueError("groups must contain at least one group name")
        return v

    @model_validator(mode="after")
    def normalize_sources(self) -> "Config":
        if self.models is None and self.llm is not None:
            self.models = Models(summary=self.llm)
        if self.models is not None and self.llm is None:
            self.llm = self.models.summary
        if self.models is None or self.llm is None:
            raise ValueError("configure a summary model with models.summary or legacy llm")

        if self.groups is not None and not self.sources.wechat.groups:
            self.sources.wechat.groups = self.groups
        self.groups = self.sources.wechat.groups

        has_wechat = bool(self.sources.wechat.groups)
        has_telegram = self.sources.telegram.enabled and bool(self.sources.telegram.chats)
        if not has_wechat and not has_telegram:
            raise ValueError("configure at least one source: sources.wechat.groups or sources.telegram.chats")
        return self

    def override_summary_model(self, model_name: str) -> None:
        """Switch summary model by alias (e.g. 'gemini'). Raises KeyError if not found."""
        alt = getattr(self, model_name, None)
        if alt is None or not isinstance(alt, LLM):
            raise KeyError(f"unknown model alias: {model_name}")
        self.models.summary = alt
        self.llm = alt


def load_config(path: Path) -> Config:
    from chat_daily_tg.paths import migrate_legacy_config_if_needed

    migrate_legacy_config_if_needed(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)
