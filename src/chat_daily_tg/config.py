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


class VisionModel(OptionalModel):
    """Image-selection thresholds live here, not as code defaults: both past
    tunings (0.65→0.8 include bar 2026-07-02, fallback floor 2026-07-14) were
    library-source edits, and vision-audit.jsonl exists precisely so these can
    be recalibrated from history without a deploy (PR #8 review X2)."""
    min_prefilter_score: float = 0.45
    min_include_score: float = 0.8
    fallback_min_score: float = 0.65


class Models(BaseModel):
    summary: LLM
    vision: VisionModel | None = None
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


class RawChannel(BaseModel):
    """A channel whose messages are pushed verbatim as per-message TG cards,
    bypassing the LLM summary entirely. `username` (without @) enables the public
    t.me link preview; omit it for private channels (media is downloaded + re-sent).
    `strip_patterns` are regexes; any message LINE matching one is removed before
    pushing (e.g. a channel's promo header/footer). `prefer_content_link` suits
    repost-style channels whose posts are mostly a bare external URL: the card then
    previews that URL (the paper/repo/tweet itself) instead of the t.me permalink,
    keeping the permalink only as a small 原文↗ jump link."""
    id: str
    name: str
    username: str | None = None
    limit: int = 500
    strip_patterns: list[str] = Field(default_factory=list)
    prefer_content_link: bool = False
    # 论坛话题路由 key（对应 ~/qwenproxy/.tg-notify-targets.json 的 topics）。
    # 默认 channels_news（频道·资讯）。找不到 key 时 resolve_tg_target 回落 DM。
    topic: str = "channels_news"


class TelegramSource(BaseModel):
    enabled: bool = False
    db_path: str = "~/Library/Application Support/tg-cli/messages.db"
    chats: list[TelegramChat] = Field(default_factory=list)
    raw_channels: list[RawChannel] = Field(default_factory=list)
    raw_card_delay_seconds: float = 1.0  # pause between card pushes to respect TG rate limits
    sync_before_export: bool = True


class BilibiliUp(BaseModel):
    """One whitelisted UP. Matching is by uid ONLY — Bilibili display names are
    mutable, so `name` is a human-readable annotation, never a match key."""
    uid: int
    name: str | None = None


class BilibiliOpencli(BaseModel):
    profile: str | None = None       # opencli --profile; None = default profile
    timeout_seconds: float = 60.0    # per opencli subprocess call


class BilibiliFetch(BaseModel):
    whitelist: list[BilibiliUp] = Field(default_factory=list)
    blacklist: list[BilibiliUp] = Field(default_factory=list)
    max_per_digest: int = 30
    # Wide window on purpose: bvid dedup makes overlap free, and a failed/slept-through
    # run is caught up by the next one instead of losing videos (design doc §12).
    lookback_hours: int = 48
    per_up_limit: int = 8            # user-videos --limit per whitelisted UP


class BilibiliDigest(BaseModel):
    topic: str = "bilibili"          # forum-topic key in ~/qwenproxy/.tg-notify-targets.json
    summary_enabled: bool = True
    cover_enabled: bool = True
    link_enabled: bool = True        # 在 B 站观看 inline-keyboard button under each card
    card_delay_seconds: float = 1.0  # pause between cards (TG rate limits)


class BilibiliSource(BaseModel):
    enabled: bool = False
    # "api": direct Bilibili Web API (no cookies/browser, works headless — Mac or r4s).
    # "opencli": local Chrome-bridge fallback, needs daemon + logged-in session.
    transport: Literal["api", "opencli"] = "api"
    opencli: BilibiliOpencli = Field(default_factory=BilibiliOpencli)
    fetch: BilibiliFetch = Field(default_factory=BilibiliFetch)
    digest: BilibiliDigest = Field(default_factory=BilibiliDigest)


class Sources(BaseModel):
    wechat: WechatSource = Field(default_factory=WechatSource)
    telegram: TelegramSource = Field(default_factory=TelegramSource)
    bilibili: BilibiliSource = Field(default_factory=BilibiliSource)


class Retry(BaseModel):
    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [5, 15, 60])


class Sanitize(BaseModel):
    enabled: bool = False


class Archive(BaseModel):
    media_retention_days: int = 14


class ImgRelay(BaseModel):
    """Ephemeral Cloudflare KV image relay for single-message rich digests.

    sendRichMessage only accepts publicly fetchable https image URLs (attach://,
    file_id, and Telegram's own file URLs are all rejected — tested 2026-07-02).
    The relay uploads the cited image to the user's own CF Workers KV under an
    unguessable key, Telegram re-hosts it at send time, and the key is deleted
    immediately after (ttl_seconds is the belt-and-braces backstop)."""
    enabled: bool = False
    account_id: str = ""
    namespace_id: str = ""
    worker_base: str = ""            # e.g. https://tg-img-relay.<sub>.workers.dev
    api_token_env: str = "CF_KV_API_TOKEN"
    ttl_seconds: int = 300


class GrowthSourceChat(BaseModel):
    id: str                          # config form, e.g. '-1001162433032'
    name: str
    limit: int = 4000                # peak observed day is ~2100 msgs; headroom on top


class GrowthWeekly(BaseModel):
    weekday: int = 5                 # Python date.weekday(): 5 = Saturday


class Growth(BaseModel):
    """成长内容挖掘：从单个群挖「个人成长」向对话段落，精华卡片发 growth topic。

    Mining is day-granular (Beijing calendar day). Cards carry verbatim quotes
    that MUST survive the miner's trust-boundary check against messages.db."""
    enabled: bool = False
    source: GrowthSourceChat | None = None
    topic: str = "growth"            # forum-topic key in ~/qwenproxy/.tg-notify-targets.json
    backfill_start: str = "2026-04-27"
    daily_quota: int = 1             # max cards pushed per day
    min_score: float = 6.0           # segments scoring below this are stored as rejected
    chunk_chars: int = 60000         # transcript chunk budget per LLM call
    min_segment_msgs: int = 6
    max_segment_msgs: int = 300
    judge_model: str | None = None   # top-level LLM alias (e.g. "grok") for the A/B
                                     # judge; None = judge with the miner's model.
    weekly: GrowthWeekly = Field(default_factory=GrowthWeekly)


class Config(BaseModel):
    groups: list[str] | None = None
    sources: Sources = Field(default_factory=Sources)
    todo: list[str] = Field(default_factory=list)
    schedule: Schedule = Field(default_factory=Schedule)
    hot_leads: HotLeads = Field(default_factory=HotLeads)
    llm: LLM | None = None
    gemini: LLM | None = None
    grok: LLM | None = None
    models: Models | None = None
    telegram: Telegram
    retry: Retry = Field(default_factory=Retry)
    sanitize: Sanitize = Field(default_factory=Sanitize)
    archive: Archive = Field(default_factory=Archive)
    img_relay: ImgRelay = Field(default_factory=ImgRelay)
    growth: Growth = Field(default_factory=Growth)
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
        has_telegram = self.sources.telegram.enabled and bool(
            self.sources.telegram.chats or self.sources.telegram.raw_channels
        )
        has_bilibili = self.sources.bilibili.enabled and bool(self.sources.bilibili.fetch.whitelist)
        if not has_wechat and not has_telegram and not has_bilibili:
            raise ValueError("configure at least one source: sources.wechat.groups, sources.telegram.chats, sources.telegram.raw_channels, or sources.bilibili")
        return self

    def resolve_model_alias(self, model_name: str) -> LLM:
        """Look up a top-level LLM alias (e.g. 'gemini', 'grok'). Raises KeyError if not found."""
        alt = getattr(self, model_name, None)
        if alt is None or not isinstance(alt, LLM):
            raise KeyError(f"unknown model alias: {model_name}")
        return alt

    def override_summary_model(self, model_name: str) -> None:
        """Switch summary model by alias (e.g. 'gemini'). Raises KeyError if not found."""
        alt = self.resolve_model_alias(model_name)
        self.models.summary = alt
        self.llm = alt


def load_config(path: Path) -> Config:
    from chat_daily_tg.paths import migrate_legacy_config_if_needed

    migrate_legacy_config_if_needed(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)
