from __future__ import annotations
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field, field_validator


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


class Telegram(BaseModel):
    bot_token_env: str
    chat_id_env: str


class Retry(BaseModel):
    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [5, 15, 60])


class Sanitize(BaseModel):
    enabled: bool = False


class Config(BaseModel):
    groups: list[str]
    todo: list[str] = Field(default_factory=list)
    schedule: Schedule = Field(default_factory=Schedule)
    hot_leads: HotLeads = Field(default_factory=HotLeads)
    llm: LLM
    telegram: Telegram
    retry: Retry = Field(default_factory=Retry)
    sanitize: Sanitize = Field(default_factory=Sanitize)

    @field_validator("groups")
    @classmethod
    def groups_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("groups must contain at least one group name")
        return v


def load_config(path: Path) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)
