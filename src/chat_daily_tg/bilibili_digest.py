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

from chat_daily_tg.bilibili_fetcher import BiliVideo
from chat_daily_tg.config import Config
from chat_daily_tg.raw_seen import SeenStore
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
                }
                headers = {"Authorization": f"Bearer {os.environ[vision.api_key_env]}"}
                with httpx.Client(timeout=vision.timeout) as c:
                    r = c.post(f"{vision.endpoint}/chat/completions", json=payload, headers=headers)
                    r.raise_for_status()
                    text = r.json()["choices"][0]["message"]["content"]
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
    """Card text WITHOUT the watch link — that ships as an inline-keyboard URL
    button under the card (bigger tap target than an <a> in the caption)."""
    meta = [escape_html(video.author)]
    if video.duration:
        meta.append(escape_html(video.duration))
    if video.publish_time:
        meta.append(video.publish_time.strftime("%m-%d %H:%M"))
    if video.view is not None:
        meta.append(f"{video.view:,}播放")
    lines = [f"<b>{escape_html(video.title)}</b>", "👤 " + " · ".join(meta)]
    if summary:
        lines.append(f"📝 {escape_html(summary)}")
    return "\n".join(lines)


def push_digest(videos: list[BiliVideo], *, sender: TelegramSender | None,
                seen: SeenStore, cfg: Config, summarizer: Summarizer | None,
                workdir: Path, no_push: bool = False) -> int:
    """Send one card per video, oldest first (topic reads chronologically).
    Returns the number of cards actually sent. no_push logs the would-be cards
    WITHOUT marking them seen, so a later real run still pushes them."""
    digest = cfg.sources.bilibili.digest
    sent = 0
    for video in reversed(videos):
        if no_push or sender is None:
            # Dry-run short-circuits before cover download / LLM spend.
            log.info("[no-push] %s %s (%s)", video.bvid, video.title, video.author)
            continue
        cover_path: Path | None = None
        if digest.cover_enabled and video.cover:
            cover_path = download_cover(video.cover, workdir / f"bili-{video.bvid}.jpg")
        summary = summarizer(video, cover_path) if summarizer else None
        caption = card_caption(video, summary)
        button = ("▶️ 在 B 站观看", video.url) if digest.link_enabled else None
        try:
            if cover_path is not None:
                try:
                    sender.send_photo(cover_path, caption=caption, parse_mode="HTML",
                                      button=button)
                except Exception as e:
                    log.warning("sendPhoto failed for %s, falling back to text: %s",
                                video.bvid, e)
                    sender.send_card(caption, link=video.url if digest.link_enabled else None,
                                     button=button)
            else:
                sender.send_card(caption, link=video.url if digest.link_enabled else None,
                                 button=button)
        except Exception as e:
            # This card failed both paths — leave it unseen so the next run
            # retries it, and keep going with the rest of the digest.
            log.error("card push failed for %s: %s", video.bvid, e)
            continue
        seen.add(video.seen_key)
        sent += 1
        time.sleep(digest.card_delay_seconds)
    return sent
