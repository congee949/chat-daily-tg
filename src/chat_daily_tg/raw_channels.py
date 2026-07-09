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
_URL_RE = re.compile(r"https?://[^\s<>]+")
# Inline Markdown link the channel author typed in the body: [label](https://…).
# The body is sent under HTML parse mode, so plain escape_html leaves this syntax
# literal — Telegram only auto-links the bare URL, showing "[label](url)" verbatim.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def escape_body_html(text: str) -> str:
    """Escape body text as Telegram HTML, converting inline Markdown links
    [label](url) into real <a> anchors in the same pass so they render clickable
    instead of literally. Text with no Markdown links is escaped exactly as
    escape_html would, so non-link bodies are unchanged."""
    parts: list[str] = []
    pos = 0
    for m in _MD_LINK_RE.finditer(text):
        parts.append(escape_html(text[pos:m.start()]))
        label = escape_html(m.group(1))
        href = escape_html(m.group(2)).replace('"', "&quot;")
        parts.append(f'<a href="{href}">{label}</a>')
        pos = m.end()
    parts.append(escape_html(text[pos:]))
    return "".join(parts)

# A Telegram album (media group) arrives as several messages that share a grouped_id,
# but tg-cli's messages.db stores no raw_json, so grouped_id is unavailable here. We
# infer the album instead: a media-only item (empty body) whose msg_id directly follows
# the previous item within this many seconds is another photo of the same post, not its
# own post. Folding them stops one album from rendering as a caption card plus N
# "🖼 媒体内容" placeholder cards.
_ALBUM_WINDOW_SECONDS = 10


def _within_album_window(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """True when two rows' timestamps are within the album burst window. A bad
    timestamp counts as "not within" so the rows stay separate posts."""
    try:
        return abs(
            parse_timestamp(a["timestamp"]).timestamp()
            - parse_timestamp(b["timestamp"]).timestamp()
        ) <= _ALBUM_WINDOW_SECONDS
    except Exception:
        return False


def _group_albums(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Collapse album items into one logical post each. Returns groups in msg_id order;
    group[0] is the head (carries the caption + permalink), the rest are the album's
    extra media-only items. Every member id is preserved so the caller can mark them all
    seen — recording only the head would stall the incremental high-water mark at the
    album's first id, re-pushing the rest as placeholders next run."""
    groups: list[list[sqlite3.Row]] = []
    for r in sorted(rows, key=lambda r: r["msg_id"]):
        if groups and not (r["content"] or "").strip():
            prev = groups[-1][-1]
            if r["msg_id"] == prev["msg_id"] + 1 and _within_album_window(prev, r):
                groups[-1].append(r)
                continue
        groups.append([r])
    return groups


def _first_external_url(text: str) -> str | None:
    """First http(s) URL in `text`, with trailing sentence punctuation/quotes trimmed.
    Used by prefer_content_link channels to preview the body's link itself. Brackets
    are left intact so URLs like ...wiki/Foo_(bar) survive."""
    m = _URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,;!?\"'")


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
    permalink = f"https://t.me/{username}/{msg_id}" if username and msg_id else None

    if not content:
        if permalink is None:
            return None  # private + media-only: nothing to render
        content_html = _MEDIA_PLACEHOLDER
    else:
        content_html = escape_body_html(content)

    ts = parse_timestamp(row["timestamp"]).astimezone(LOCAL_TZ).strftime("%H:%M")
    header = f"📢 <b>{escape_html(channel.name)}</b> · {ts}"
    fwd = ""
    if row["raw_json"] and "fwd" in str(row["raw_json"]).lower():
        fwd = " <i>[转发]</i>"

    # Repost-style channels (prefer_content_link): the body is usually a bare external
    # URL — a paper/repo/tweet. Preview THAT url (the rich card the user already sees in
    # the channel) instead of the t.me permalink, whose preview is just a "VIEW MESSAGE"
    # jump-into-channel button. Keep the permalink as a small 原文↗ link so reactions/
    # comments stay one tap away. Falls back to the permalink preview when the body has
    # no URL (media-only / plain text).
    preview_link = permalink
    permalink_suffix = ""
    if channel.prefer_content_link and content:
        ext = _first_external_url(content)
        if ext:
            preview_link = ext
            if permalink:
                permalink_suffix = f' · <a href="{escape_html(permalink)}">原文↗</a>'

    text_html = f"{header}{fwd}{permalink_suffix}\n\n{content_html}"
    return Card(text_html=text_html, link=preview_link)


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
    private_attempted = 0
    private_failed = 0
    for ch in channels:
        hwm = seen.max_msg_id(ch.id) if incremental else 0
        # Private channels (no public username) get the media-download path: Telegram
        # can't render a preview card for t.me/c links, so we download media via the
        # user session and re-upload it through the bot.
        if not (ch.username or "").lstrip("@"):
            private_attempted += 1
            try:
                from chat_daily_tg.private_media import push_private_channel
                total += push_private_channel(
                    channel=ch, since=since, until=until,
                    out_dir=archive_dir / f"rawmedia-{_safe(ch.name)}",
                    sender=sender, limit=ch.limit, seen=seen, min_id=hwm,
                    delay_seconds=delay_seconds, no_push=no_push,
                )
            except Exception as e:
                private_failed += 1
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

        # Fold album items into one card each, then build per-group so one malformed row
        # (e.g. bad timestamp) skips itself instead of aborting the whole channel. Each
        # card carries every member msg_id so all of them get marked seen on send.
        cards: list[tuple[list[int], Card]] = []
        for group in _group_albums(rows):
            head = group[0]
            try:
                c = build_card(head, ch)
            except Exception as e:
                log.warning("raw card build skipped (%s msg %s): %s", ch.name, head["msg_id"], e)
                continue
            if c is not None:
                cards.append(([r["msg_id"] for r in group], c))
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

        for ids, c in cards:
            if SeenStore.key(ch.id, ids[0]) in seen:  # head id identifies the card
                continue
            try:
                sender.send_card(c.text_html, link=c.link)
            except Exception as e:
                log.warning("raw card push failed (%s): %s", ch.name, e)
                continue
            # write-after-send: a crash re-tries rather than drops. Record EVERY album
            # item, or the incremental high-water mark stalls at the head id.
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            total += 1
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    # All private channels failing together is the signature of a broken shared
    # dependency (e.g. the kabi-tg-cli interpreter vanished) — not bad luck on one
    # channel. Surface it instead of returning a quiet 0 (review finding #20).
    if private_attempted and private_failed == private_attempted:
        from chat_daily_tg.notifier import notify_failure
        notify_failure(
            "chat-daily-tg 私有频道全部失败",
            f"{private_failed}/{private_attempted} 个私有频道转发失败"
            "（可能 kabi-tg-cli 解释器失效），见日志。",
        )
    return total


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60]
