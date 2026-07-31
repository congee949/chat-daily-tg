"""Render and push Bilibili digest cards to the Telegram forum topic.

One card per video: cover photo + HTML caption (title / UP / duration / AI
one-liner / watch link). Failure isolation per card: summary failure → card
ships without the 📝 line; cover download or sendPhoto failure → text card via
send_card (rich link preview of the video URL). A video is marked seen ONLY
after its card actually sent, so a crash mid-digest retries the remainder next
run instead of dropping it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from typing import Callable

import httpx

from chat_daily_tg.bilibili_fetcher import BiliArticle, BiliVideo, BilibiliContent
from chat_daily_tg.config import Config
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.sent_ledger import append_message_ids
from chat_daily_tg.tg_sender import TelegramSender, escape_html
from chat_daily_tg.vision import _image_data_url

log = logging.getLogger(__name__)

Summarizer = Callable[[BiliVideo, Path | None], str | None]

_SUMMARY_PROMPT = (
    "你是视频导读助手。基于给出的 B 站视频标题、简介{cover_hint}，"
    "用一句话（不超过 40 字）概括视频核心内容，帮读者判断是否值得看。"
    "只输出这一句话，不要任何前缀或引号。\n\n"
    "标题：{title}\n简介：{description}"
)


def build_summarizer(cfg: Config) -> Summarizer | None:
    """Tiered one-line summary: vision model on cover+title+desc when
    models.vision is enabled; else the text summary LLM on metadata alone.
    Returns None when summaries are disabled. Never raises from the returned
    callable — a summary failure just yields None."""
    if not cfg.sources.bilibili.digest.summary_enabled:
        return None
    vision = cfg.models.vision if cfg.models else None
    use_vision = bool(vision and vision.enabled and vision.api_key_env in os.environ)

    def summarize(video: BiliVideo, cover_path: Path | None) -> str | None:
        desc = (video.description or "")[:500]
        try:
            if use_vision and cover_path is not None:
                prompt = _SUMMARY_PROMPT.format(cover_hint="和封面图", title=video.title,
                                                description=desc)
                payload = {
                    "model": vision.model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url(cover_path)}},
                    ]}],
                    "max_tokens": 200,
                    # gemini-3.5-flash 的内部思考按 max_tokens 计费：默认档会把
                    # 预算吃到 finish=length，content 只剩截断碎渣（如") * **"）。
                    # 一句话摘要无需思考，显式关闭。
                    "reasoning_effort": "none",
                }
                headers = {"Authorization": f"Bearer {os.environ[vision.api_key_env]}"}
                with httpx.Client(timeout=vision.timeout) as c:
                    r = c.post(f"{vision.endpoint}/chat/completions", json=payload, headers=headers)
                    r.raise_for_status()
                    choice = r.json()["choices"][0]
                    if choice.get("finish_reason") == "length":
                        # 截断产物必是碎渣——宁可无摘要行，不推垃圾。
                        log.warning("summary truncated for %s, dropping", video.bvid)
                        return None
                    text = choice["message"]["content"]
            else:
                from chat_daily_tg.llm_client import LLMClient
                m = cfg.models.summary
                llm = LLMClient(endpoint=m.endpoint, model=m.model,
                                api_key=os.environ[m.api_key_env],
                                max_tokens=500, timeout=m.timeout,
                                extra_body=m.extra_body)
                prompt = _SUMMARY_PROMPT.format(cover_hint="", title=video.title,
                                                description=desc)
                text, _ = llm.chat(prompt)
            line = " ".join(text.strip().split())
            return line[:120] or None
        except Exception as e:
            log.warning("summary failed for %s: %s", video.bvid, e)
            return None

    return summarize


def download_cover(url: str, dest: Path) -> Path | None:
    """Best-effort cover download; None on any failure (card falls back to text).

    trust_env=False: hdslb.com is Bilibili CDN — same direct-connection invariant
    as the fetcher (the guard's HTTPS_PROXY would route it via an overseas exit)."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "Mozilla/5.0",
                                   "Referer": "https://www.bilibili.com/"}) as c:
            r = c.get(url)
            r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as e:
        log.warning("cover download failed (%s): %s", url, e)
        return None


def card_caption(video: BiliVideo, summary: str | None) -> str:
    """Card text: title / UP / optional summary only.

    Watch UX uses the inline-keyboard URL button (bigger tap target).
    URL is not printed in caption (cleaner card); Podcast 👍 handoff
    resolves URL via media_sent_ledger write-after-send.
    """
    meta = [escape_html(video.author)]
    if video.duration:
        meta.append(escape_html(video.duration))
    lines = [f"<b>{escape_html(video.title)}</b>", "👤 " + " · ".join(meta)]
    if (video.subscription_name and video.publisher_uid is not None
            and video.publisher_uid != video.uid):
        lines.append(f"🔖 来自订阅：{escape_html(video.subscription_name)}（联合投稿）")
    if summary:
        lines.append(f"📝 {escape_html(summary)}")
    return "\n".join(lines)


def article_card_caption(article: BiliArticle) -> str:
    """Article card text uses list metadata only; never invent a preview."""
    meta = [escape_html(article.author)] if article.author else []
    if article.publish_time is not None:
        meta.append(article.publish_time.strftime("%m-%d %H:%M"))
    lines = ["📄 专栏", f"<b>{escape_html(article.title)}</b>"]
    if meta:
        lines.append("👤 " + " · ".join(meta))
    if article.summary:
        lines.append(f"📝 {escape_html(article.summary)}")
    lines.append("❤️ 标记后发送到 Podcast4Bot 分析")
    return "\n".join(lines)


def _message_ids(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return [int(mid) for mid in value if mid is not None]


def push_digest(contents: list[BilibiliContent], *, sender: TelegramSender | None,
                seen: SeenStore, cfg: Config, summarizer: Summarizer | None,
                workdir: Path, no_push: bool = False) -> int:
    """Send one card per content item, oldest first (topic reads chronologically).
    Returns the number of cards actually sent. no_push logs the would-be cards
    WITHOUT marking them seen, so a later real run still pushes them."""
    digest = cfg.sources.bilibili.digest
    sent = 0
    for content in reversed(contents):
        if no_push or sender is None:
            # Dry-run short-circuits before cover download / LLM spend.
            log.info("[no-push] %s %s (%s)", content.seen_key, content.title, content.author)
            continue
        cover_path: Path | None = None
        if digest.cover_enabled and content.cover:
            cover_path = download_cover(
                content.cover, workdir / f"bili-{content.kind}-{content.content_id}.jpg"
            )
        if isinstance(content, BiliVideo):
            summary = summarizer(content, cover_path) if summarizer else None
            caption = card_caption(content, summary)
            # 视频 CTA 继续走自有跳转页，保持既有 PiliPlus 唤起体验。
            button = (
                ("▶️ 在 B 站观看", f"https://kanban.congeelife.top:8443/b/{content.bvid}")
                if digest.link_enabled else None
            )
        else:
            caption = article_card_caption(content)
            button = ("📖 阅读全文", content.url) if digest.link_enabled else None
        msg_ids: list[int] = []
        try:
            if cover_path is not None:
                try:
                    mid = sender.send_photo(cover_path, caption=caption, parse_mode="HTML",
                                            button=button)
                    msg_ids = _message_ids(mid)
                except Exception as e:
                    log.warning("sendPhoto failed for %s, falling back to text: %s",
                                content.content_id, e)
                    ids = sender.send_card(caption, link=content.url if digest.link_enabled else None,
                                           button=button)
                    msg_ids = _message_ids(ids)
            else:
                ids = sender.send_card(caption, link=content.url if digest.link_enabled else None,
                                       button=button)
                msg_ids = _message_ids(ids)
        except Exception as e:
            # This card failed both paths — leave it unseen so the next run
            # retries it, and keep going with the rest of the digest.
            log.error("card push failed for %s: %s", content.content_id, e)
            continue
        if not msg_ids:
            log.error("card push returned no message id for %s; leaving unseen", content.content_id)
            continue
        # Write-after-send: message_id → canonical URL for Podcast thumbs-up handoff.
        try:
            written = append_message_ids(
                msg_ids,
                chat_id=sender.chat_id,
                thread_id=getattr(sender, "message_thread_id", None),
                url=content.url,
                producer="bilibili",
                content_id=content.seen_key,
            )
            if written != len(msg_ids):
                log.error("sent_ledger incomplete for %s: %s/%s message ids", content.content_id,
                          written, len(msg_ids))
        except Exception as e:
            # The visible card already exists.  Do not resend it, but make the
            # reaction-routing loss highly visible.
            log.error("sent_ledger write failed for %s: %s", content.content_id, e)
        seen.add(content.seen_key)
        sent += 1
        time.sleep(digest.card_delay_seconds)
    return sent
