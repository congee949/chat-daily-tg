"""Verbatim channel → Telegram card stage.

For channels listed under `sources.telegram.raw_channels`, every message in the
coverage window is pushed as its own X-Monitor-style card — full text, no LLM
summary, no truncation. Public channels (with a `username`) get a t.me link
preview + 打开原文 button; private channels degrade to plain text.

This stage runs AFTER the summary push and is wrapped by the caller so any failure
here only logs/notifies and never affects the already-delivered daily summary.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from chat_daily_tg.config import RawChannel
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.telegram_exporter import (
    LOCAL_TZ,
    parse_timestamp,
    read_messages,
    sync_chat,
)
from chat_daily_tg.tg_sender import TelegramSender, escape_html

log = logging.getLogger(__name__)

# Placeholder text for a media-only message (empty body). For a public channel the
# link preview still renders the media; the text just can't be empty for sendMessage.
_MEDIA_PLACEHOLDER = "🖼 （媒体内容，见下方预览 / 原文）"


@dataclass(frozen=True)
class Card:
    text_html: str
    link: str | None


_TAG_RE = re.compile(r"<[^>]+>")


def visible_text(html: str) -> str:
    """Strip HTML tags + unescape entities → the text Telegram counts for length limits."""
    return _TAG_RE.sub("", html).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def strip_promo_lines(text: str, patterns: list[str]) -> str:
    """Drop whole lines matching any of `patterns` (regex search), e.g. a channel's
    promo header/footer like '🌸 示例频道 · 备用频道 · 投稿通道'. Collapses the blank
    lines left behind and trims. Returns text unchanged when no patterns are set."""
    if not patterns or not text:
        return text
    compiled = [re.compile(p) for p in patterns]
    kept = [line for line in text.splitlines() if not any(c.search(line) for c in compiled)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def strip_promo_lines_html(html: str, patterns: list[str]) -> str:
    """Like strip_promo_lines but for Telegram HTML: a line is dropped when its VISIBLE
    text (tags stripped) matches a pattern, so the promo footer line — links and all —
    is removed while a kept line's <a>/<b> markup (e.g. a clickable news source) stays."""
    if not patterns or not html:
        return html
    compiled = [re.compile(p) for p in patterns]
    kept = [line for line in html.splitlines()
            if not any(c.search(visible_text(line)) for c in compiled)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def build_card(row: sqlite3.Row, channel: RawChannel) -> Card | None:
    """Build one verbatim card from a message row. Returns None only for a private
    channel message with no text (nothing to show, no preview to fall back on)."""
    content = strip_promo_lines((row["content"] or "").strip(), channel.strip_patterns)
    username = (channel.username or "").lstrip("@") or None
    msg_id = row["msg_id"]
    link = f"https://t.me/{username}/{msg_id}" if username and msg_id else None

    if not content:
        if link is None:
            return None  # private + media-only: nothing to render
        content_html = _MEDIA_PLACEHOLDER
    else:
        content_html = escape_html(content)

    ts = parse_timestamp(row["timestamp"]).astimezone(LOCAL_TZ).strftime("%H:%M")
    header = f"📢 <b>{escape_html(channel.name)}</b> · {ts}"
    fwd = ""
    if row["raw_json"] and "fwd" in str(row["raw_json"]).lower():
        fwd = " <i>[转发]</i>"
    text_html = f"{header}{fwd}\n\n{content_html}"
    return Card(text_html=text_html, link=link)


def push_raw_channel_cards(
    *,
    channels: list[RawChannel],
    since: str,
    until: str,
    db_path: str | Path,
    sender: TelegramSender,
    archive_dir: Path,
    seen_path: str | Path,
    sync_before_export: bool = True,
    delay_seconds: float = 1.0,
    no_push: bool = False,
    incremental: bool = False,
) -> int:
    """Export each raw channel's window and push every message as a card.

    Returns the number of cards pushed. A failure on a single channel/message is
    logged and skipped; it never aborts the remaining channels. Already-pushed message
    ids (tracked in `seen_path`) are skipped, so re-runs/retries don't duplicate.
    incremental=True (the 2-hourly forwarder) fetches only messages newer than each
    channel's high-water mark, so high-volume private channels aren't re-downloaded."""
    seen = SeenStore(seen_path)
    total = 0
    for ch in channels:
        hwm = seen.max_msg_id(ch.id) if incremental else 0
        # Private channels (no public username) get the media-download path: Telegram
        # can't render a preview card for t.me/c links, so we download media via the
        # user session and re-upload it through the bot.
        if not (ch.username or "").lstrip("@"):
            try:
                from chat_daily_tg.private_media import push_private_channel
                total += push_private_channel(
                    channel=ch, since=since, until=until,
                    out_dir=archive_dir / f"rawmedia-{_safe(ch.name)}",
                    sender=sender, limit=ch.limit, seen=seen, min_id=hwm,
                    delay_seconds=delay_seconds, no_push=no_push,
                )
            except Exception as e:
                log.warning("private channel push failed for %s: %s", ch.name, e)
            continue

        try:
            if sync_before_export:
                sync_chat(ch.id, limit=ch.limit)
            rows = read_messages(
                db_path=Path(db_path).expanduser(),
                chat_id=ch.id,
                since=since,
                until=until,
                limit=ch.limit,
                min_msg_id=hwm,
            )
        except Exception as e:
            log.warning("raw channel export failed for %s: %s", ch.name, e)
            continue

        # Build per-row so one malformed row (e.g. bad timestamp) skips itself instead
        # of aborting the whole channel.
        cards: list[tuple[int, Card]] = []
        for r in rows:
            try:
                c = build_card(r, ch)
            except Exception as e:
                log.warning("raw card build skipped (%s msg %s): %s", ch.name, r["msg_id"], e)
                continue
            if c is not None:
                cards.append((r["msg_id"], c))
        log.info("raw channel %s: %d msgs → %d cards", ch.name, len(rows), len(cards))

        # Archive verbatim cards for auditability (always, even with --no-push).
        archive_path = archive_dir / f"rawcard-{_safe(ch.name)}.md"
        archive_path.write_text(
            "\n\n---\n\n".join(
                (c.text_html + (f"\n\n[原文] {c.link}" if c.link else "")) for _, c in cards
            )
            or "(无消息)",
            encoding="utf-8",
        )

        if no_push:
            continue

        for msg_id, c in cards:
            key = SeenStore.key(ch.id, msg_id)
            if key in seen:
                continue
            try:
                sender.send_card(c.text_html, link=c.link)
            except Exception as e:
                log.warning("raw card push failed (%s): %s", ch.name, e)
                continue
            seen.add(key)  # write-after-send: a crash re-tries rather than drops
            total += 1
            if delay_seconds > 0:
                time.sleep(delay_seconds)
    return total


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60]
