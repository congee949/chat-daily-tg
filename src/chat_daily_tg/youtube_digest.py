"""Render and push YouTube digest cards to the Telegram forum topic.

One card per video: cover photo + HTML caption (title / channel / duration /
AI one-liner / watch button). Mirrors bilibili_digest deliberately — the two
pipelines stay separate so a YouTube change can never regress the working
Bilibili path. Failure isolation per card: summary failure → card ships
without the 📝 line; cover download or sendPhoto failure → text card via
send_card. A video is marked seen ONLY after its card actually sent
(write-after-send), so a crash mid-digest retries the remainder next run.

Cover downloads ride the proxy (trust_env default True) — i.ytimg.com is as
unreachable from a China exit as YouTube itself; this is the REVERSE of
bilibili_digest.download_cover's forced direct connection.
"""
from __future__ import annotations

from chat_daily_tg.tg_sender import delivery_identity
from contextlib import ExitStack
import logging
import os
from pathlib import Path
import time
from typing import Callable

import httpx

from chat_daily_tg.config import Config
from chat_daily_tg.resource_util import enter_optional_client
from chat_daily_tg.raw_seen import SeenStore, mark_after_send
from chat_daily_tg.sent_ledger import append_message_ids
from chat_daily_tg.tg_sender import TelegramSender, escape_html
from chat_daily_tg.vision import _image_data_url
from chat_daily_tg.youtube_fetcher import YtVideo

log = logging.getLogger(__name__)

Summarizer = Callable[[YtVideo, Path | None], str | None]

_SUMMARY_PROMPT = (
    "你是视频导读助手。基于给出的 YouTube 视频标题、简介{cover_hint}，"
    "用一句话（不超过 40 字）概括视频核心内容，帮读者判断是否值得看。"
    "视频可能是英文内容，摘要一律用中文。只输出这一句话，不要任何前缀或引号。\n\n"
    "标题：{title}\n简介：{description}"
)


def build_summarizer(cfg: Config, *, stack: ExitStack | None = None) -> Summarizer | None:
    """Tiered one-line summary: vision model on cover+title+desc when
    models.vision is enabled; else the text summary LLM on metadata alone.
    Returns None when summaries are disabled. Never raises from the returned
    callable — a summary failure just yields None."""
    if not cfg.sources.youtube.digest.summary_enabled:
        return None
    vision = cfg.models.vision if cfg.models else None
    use_vision = bool(vision and vision.enabled and vision.api_key_env in os.environ)

    vision_client = None
    text_client = None

    def summarize(video: YtVideo, cover_path: Path | None) -> str | None:
        nonlocal vision_client, text_client
        desc = (video.description or "")[:500]
        try:
            # A caller-owned stack shares pools across the whole digest.
            # Standalone calls still close every resource before returning.
            with ExitStack() as local_stack:
                owner = stack if stack is not None else local_stack
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
                        # Model-specific request controls, including reasoning effort,
                        # come from the active vision route instead of being hard-coded.
                        **vision.extra_body,
                    }
                    headers = {"Authorization": f"Bearer {os.environ[vision.api_key_env]}"}
                    c = vision_client
                    if c is None:
                        c = enter_optional_client(owner, httpx.Client(timeout=vision.timeout))
                        if stack is not None:
                            vision_client = c
                    r = c.post(f"{vision.endpoint}/chat/completions", json=payload, headers=headers)
                    r.raise_for_status()
                    choice = r.json()["choices"][0]
                    if choice.get("finish_reason") == "length":
                        # 截断产物必是碎渣——宁可无摘要行，不推垃圾。
                        log.warning("summary truncated for %s, dropping", video.video_id)
                        return None
                    text = choice["message"]["content"]
                else:
                    from chat_daily_tg.llm_client import LLMClient
                    m = cfg.models.summary
                    llm = text_client
                    if llm is None:
                        llm = enter_optional_client(owner, LLMClient(
                            endpoint=m.endpoint, model=m.model,
                            api_key=os.environ[m.api_key_env], max_tokens=500,
                            timeout=m.timeout, extra_body=m.extra_body,
                        ))
                        if stack is not None:
                            text_client = llm
                    prompt = _SUMMARY_PROMPT.format(cover_hint="", title=video.title,
                                                    description=desc)
                    text, _ = llm.chat(prompt)
            line = " ".join(text.strip().split())
            return line[:120] or None
        except Exception as e:
            log.warning("summary failed for %s: %s", video.video_id, e)
            return None

    return summarize


def _cover_client() -> httpx.Client:
    return httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})


def download_cover(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path | None:
    """Best-effort cover download; None on any failure (card falls back to
    text). trust_env stays True: i.ytimg.com needs the proxy on r4s —
    opposite invariant to the Bilibili CDN's forced direct connection."""
    try:
        with ExitStack() as local_stack:
            c = client if client is not None else local_stack.enter_context(_cover_client())
            r = c.get(url)
            r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as e:
        log.warning("cover download failed (%s): %s", url, e)
        return None


def card_caption(video: YtVideo, summary: str | None) -> str:
    """Card text: title / channel / optional summary only.

    Watch UX uses the inline-keyboard URL button (bigger tap target).
    URL is not printed in caption (cleaner card); Podcast 👍 handoff
    resolves URL via media_sent_ledger write-after-send.
    """
    meta = [escape_html(video.author)]
    if video.duration:
        meta.append(escape_html(video.duration))
    lines = [f"<b>{escape_html(video.title)}</b>", "👤 " + " · ".join(meta)]
    if summary:
        lines.append(f"📝 {escape_html(summary)}")
    return "\n".join(lines)


def push_digest(videos: list[YtVideo], *, sender: TelegramSender | None,
                seen: SeenStore, cfg: Config, summarizer: Summarizer | None,
                workdir: Path, no_push: bool = False) -> int:
    """Send one card per video, oldest first (topic reads chronologically).
    Returns the number of cards actually sent. no_push logs the would-be cards
    WITHOUT marking them seen, so a later real run still pushes them."""
    digest = cfg.sources.youtube.digest
    sent = 0
    with ExitStack() as resources:
        cover_client = None
        for video in reversed(videos):
            with delivery_identity('youtube', 'youtube:'+video.video_id):
                if no_push or sender is None:
                    # Dry-run short-circuits before cover download / LLM spend.
                    log.info("[no-push] %s %s (%s)", video.video_id, video.title, video.author)
                    continue
                cover_path: Path | None = None
                if digest.cover_enabled and video.cover:
                    try:
                        if cover_client is None:
                            cover_client = enter_optional_client(resources, _cover_client())
                        cover_path = download_cover(
                            video.cover, workdir / f"yt-{video.video_id}.jpg", client=cover_client
                        )
                    except Exception as exc:
                        log.warning("cover unavailable; sending text card: %s", exc)
                summary = video.selection_note or (summarizer(video, cover_path) if summarizer else None)
                caption = card_caption(video, summary)
                # 直链即可：iOS/Android 的 TG 点 youtube.com 链接由系统 universal link
                # 唤起 YouTube app，无需 B 站那样的自有域名跳转页。
                button = ("▶️ 在 YouTube 观看", video.url) if digest.link_enabled else None
                msg_ids: list[int] = []
                from chat_daily_tg.tg_sender import AmbiguousDeliveryError
                try:
                    if cover_path is not None:
                        try:
                            mid = sender.send_photo(cover_path, caption=caption, parse_mode="HTML",
                                                    button=button)
                            if mid is not None:
                                msg_ids = [mid] if isinstance(mid, int) else list(mid)
                        except AmbiguousDeliveryError:
                            raise
                        except Exception as e:
                            log.warning("sendPhoto failed for %s, falling back to text: %s",
                                        video.video_id, e)
                            ids = sender.send_card(caption, link=video.url if digest.link_enabled else None,
                                                   button=button)
                            msg_ids = list(ids or [])
                    else:
                        ids = sender.send_card(caption, link=video.url if digest.link_enabled else None,
                                               button=button)
                        msg_ids = list(ids or [])
                except AmbiguousDeliveryError as e:
                    if not mark_after_send(seen, video.seen_key):
                        log.error("seen persist failed after ambiguous send for %s", video.seen_key)
                    try:
                        from chat_daily_tg import dedup_journal
                        dedup_journal.record({
                            "layer": "delivery", "action": "ambiguous",
                            "reason": "telegram_transport_timeout", "method": e.method,
                            "producer": "youtube", "content_id": video.seen_key,
                            "url": video.url,
                        })
                    except Exception:
                        pass
                    try:
                        from chat_daily_tg.notifier import notify_failure
                        notify_failure(
                            "chat-daily-tg YouTube投递结果待确认",
                            f"{video.seen_key} 的 {e.method} 响应超时；已停止自动重试，请核对后手动补。",
                        )
                    except Exception:
                        pass
                    log.error("ambiguous youtube delivery terminalized (%s via %s)",
                              video.seen_key, e.method)
                    continue
                except Exception as e:
                    # This card failed both paths — leave it unseen so the next run
                    # retries it, and keep going with the rest of the digest.
                    log.error("card push failed for %s: %s", video.video_id, e)
                    continue
                if not msg_ids:
                    log.error("card push returned no message id for %s; leaving unseen", video.video_id)
                    continue
                # Write-after-send: message_id → canonical URL for Podcast thumbs-up handoff.
                try:
                    append_message_ids(
                        msg_ids,
                        chat_id=sender.chat_id,
                        thread_id=getattr(sender, "message_thread_id", None),
                        url=video.url,
                        producer="youtube",
                        content_id=video.seen_key,
                    )
                except Exception as e:
                    log.warning("sent_ledger write failed for %s: %s", video.video_id, e)
                if not mark_after_send(seen, video.seen_key):
                    log.error("seen persist failed after send for %s; in-memory/overflow/ledger must suppress retry",
                              video.seen_key)
                sent += 1
                time.sleep(digest.card_delay_seconds)
    return sent
