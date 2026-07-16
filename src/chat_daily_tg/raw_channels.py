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


def matches_exclude_patterns(text: str, patterns: list[str]) -> bool:
    """Return True when a whole post should be suppressed.

    Invalid operator-supplied regexes are ignored (and logged) so one typo cannot
    stop an entire channel's delivery.
    """
    for pattern in patterns:
        try:
            if re.search(pattern, text or ""):
                return True
        except re.error as exc:
            log.warning("invalid raw-channel exclude regex %r ignored: %s", pattern, exc)
    return False


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


def _dedup_skip(content_plain: str, ch: RawChannel, ids: list[int],
                seen: SeenStore, content_store) -> bool:
    """L1 content-dedup gate. True = suppress this card (already journaled and
    marked seen). Any internal failure returns False — dedup must never block
    delivery (投递优先于完美)."""
    if content_store is None or not ch.dedup:
        return False
    try:
        from chat_daily_tg.content_seen import check_duplicate
        d = check_duplicate(content_plain, store=content_store)
        if not d.skip:
            return False
        log.info("skip content-dup (%s msg %s): %s hit ← %s msg %s @ %s",
                 ch.name, ids[0], d.reason,
                 d.detail.get("matched_channel", "?"),
                 d.detail.get("matched_msg_id", "?"),
                 d.detail.get("matched_sent_at", "?"))
        try:
            from chat_daily_tg import dedup_journal
            dedup_journal.record({
                "layer": "L1", "action": "skip", "reason": d.reason,
                "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
                "text_head": content_plain[:120], **d.detail,
            })
        except Exception:
            pass  # journaling failure never blocks the (already logged) decision
        # Same terminal semantics as excluded_ids: advance the high-water mark.
        for mid in ids:
            seen.add(SeenStore.key(ch.id, mid))
        return True
    except Exception as e:
        log.warning("content dedup check failed (%s msg %s), delivering: %s",
                    ch.name, ids[0], e)
        return False


def _dedup_register(content_plain: str, ch: RawChannel, ids: list[int],
                    content_store) -> None:
    """Write-after-send fingerprint registration (same crash semantics as SeenStore:
    a crash between send and register re-delivers rather than drops)."""
    if content_store is None or not ch.dedup:
        return
    try:
        from chat_daily_tg.content_seen import fingerprints_for
        content_store.register(fingerprints_for(content_plain), ch.id, ids[0], ch.name)
    except Exception as e:
        log.warning("content dedup register failed (%s msg %s): %s", ch.name, ids[0], e)


def _l2_check(topic_gate, ch: RawChannel, content_plain: str, ids: list[int],
              seen: SeenStore) -> tuple[bool, str, object]:
    """L2 topic-gate decision, shared by the public and private send paths.
    Returns (skip, annotation_html, verdict). skip=True means the card was
    journaled (with its own chat_id:msg_id for --resend) and marked seen.
    Any failure returns (False, "", None) — deliver."""
    if topic_gate is None or not ch.dedup:
        return False, "", None
    try:
        v = topic_gate.assess(content_plain, ref={
            "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
        })
        if v.action == "skip":
            log.info("skip topic-dup (%s msg %s): sim=%.2f vs msg %s",
                     ch.name, ids[0], v.similarity, v.matched_msg_id)
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            return True, "", v
        if v.action == "annotate" and v.matched_msg_id:
            return False, topic_gate.annotation_html(v.matched_msg_id), v
        return False, "", v
    except Exception as e:
        log.warning("topic gate assess failed (%s msg %s): %s", ch.name, ids[0], e)
        return False, "", None


def _l2_register(topic_gate, ch: RawChannel, content_plain: str,
                 sent_ids: list[int] | None, sender, verdict) -> None:
    """Write-after-send into the delivered index — but ONLY when the send
    actually landed in the indexed forum group. resolve_tg_target falls back
    to the DM on a missing topic key, and DM message ids live in a different
    id-space: registering them would collide with real forum PKs and mint
    deep links into the wrong chat."""
    if topic_gate is None or not ch.dedup or not sent_ids:
        return
    try:
        target = str(getattr(sender, "chat_id", ""))
        if target.removeprefix("-100") != topic_gate.group_internal_id:
            return
        topic_gate.register_sent(
            sent_ids, content_plain, "chatdaily_raw",
            thread_id=getattr(sender, "message_thread_id", None),
            vector=(verdict.vector if verdict is not None else None),
        )
    except Exception as e:
        log.warning("delivered-index register failed (%s): %s", ch.name, e)


def resend_raw_card(*, channel: RawChannel, msg_id: int, db_path: str | Path,
                    sender: TelegramSender, seen_path: str | Path) -> bool:
    """--resend escape hatch: rebuild and send ONE card, bypassing SeenStore, the
    high-water mark and every dedup layer. The recovery path for a wrong
    suppression (the journal/archive tell you the chat_id:msg_id to resend).
    Public-channel text path only — private media posts need a manual re-dump."""
    import sqlite3 as _sq
    from chat_daily_tg.telegram_exporter import canonical_chat_ids
    ids = sorted(canonical_chat_ids(channel.id))
    marks = ",".join("?" for _ in ids)
    conn = _sq.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    row = conn.execute(
        f"SELECT * FROM messages WHERE chat_id IN ({marks}) AND msg_id=?",
        [*ids, msg_id],
    ).fetchone()
    if row is None:
        log.error("resend: msg %s not found in messages.db for %s", msg_id, channel.name)
        return False
    card = build_card(row, channel)
    if card is None:
        log.error("resend: msg %s renders to no card (media-only private post?)", msg_id)
        return False
    sender.send_card(card.text_html, link=card.link)
    SeenStore(seen_path).add(SeenStore.key(channel.id, msg_id))
    log.info("resend: %s msg %s re-delivered", channel.name, msg_id)
    return True


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
    content_store=None,   # content_seen.ContentSeenStore | None (L1 dedup)
    topic_gate=None,      # topic_dedup.TopicDedupGate | None (L2 dedup)
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
                    content_store=content_store, topic_gate=topic_gate,
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
        # content_plain (promo-stripped body, no header) rides along for the dedup
        # layers — fingerprinting the rendered HTML would bake the per-channel header
        # into the identity and defeat cross-channel matching.
        cards: list[tuple[list[int], Card, str]] = []
        excluded_ids: list[int] = []
        excluded_posts: list[tuple[int, str]] = []  # (head_id, text head) for the journal
        for group in _group_albums(rows):
            head = group[0]
            ids = [r["msg_id"] for r in group]
            if matches_exclude_patterns((head["content"] or "").strip(), ch.exclude_patterns):
                excluded_ids.extend(ids)
                excluded_posts.append((ids[0], (head["content"] or "")[:120]))
                continue
            try:
                c = build_card(head, ch)
            except Exception as e:
                log.warning("raw card build skipped (%s msg %s): %s", ch.name, head["msg_id"], e)
                continue
            if c is not None:
                content_plain = strip_promo_lines((head["content"] or "").strip(), ch.strip_patterns)
                cards.append((ids, c, content_plain))
        log.info("raw channel %s: %d msgs → %d cards (%d filtered)",
                 ch.name, len(rows), len(cards), len(excluded_ids))

        # Archive verbatim cards for auditability (always, even with --no-push).
        # Written BEFORE the send loop, so a dedup-suppressed card still leaves its
        # full text here — the recovery/audit trail for a wrong suppression.
        archive_path = archive_dir / f"rawcard-{_safe(ch.name)}.md"
        archive_path.write_text(
            "\n\n---\n\n".join(
                (c.text_html + (f"\n\n[原文] {c.link}" if c.link else "")) for _, c, _ in cards
            )
            or "(无消息)",
            encoding="utf-8",
        )

        if no_push:
            continue

        # A configured exclusion is a successful terminal decision, not a send
        # failure. Record every member so incremental polling does not fetch the
        # same intentionally suppressed post forever. Journaled like every other
        # suppression: an overbroad exclude regex is otherwise untraceable —
        # excluded posts never reach the rawcard archive, and --resend's
        # documented recovery flow starts from the journal.
        for mid in excluded_ids:
            seen.add(SeenStore.key(ch.id, mid))
        for head_id, text_head in excluded_posts:
            try:
                from chat_daily_tg import dedup_journal
                dedup_journal.record({
                    "layer": "L1", "action": "skip", "reason": "exclude_pattern",
                    "chat_id": ch.id, "msg_id": head_id, "channel": ch.name,
                    "text_head": text_head,
                })
            except Exception:
                pass

        if topic_gate is not None and ch.dedup and cards:
            try:  # one embed batch per channel; failure → gate goes offline, all deliver.
                # Only unseen cards — a catch-up re-run must not re-embed what it
                # is about to skip on the seen check anyway.
                unseen = [cp for card_ids, _, cp in cards
                          if SeenStore.key(ch.id, card_ids[0]) not in seen]
                if unseen:
                    topic_gate.prepare(unseen)
            except Exception as e:
                log.warning("topic gate prepare failed (%s): %s", ch.name, e)

        for ids, c, content_plain in cards:
            if SeenStore.key(ch.id, ids[0]) in seen:  # head id identifies the card
                continue

            if _dedup_skip(content_plain, ch, ids, seen, content_store):
                continue

            l2_skip, annotation, l2_verdict = _l2_check(
                topic_gate, ch, content_plain, ids, seen)
            if l2_skip:
                continue
            text_html = c.text_html
            if annotation:
                # build_card's layout contract: one header line, then "\n\n",
                # then the body (raw_channels.py:194). The annotation becomes a
                # second header line. Contract is pinned by tests.
                head_part, sep, body = text_html.partition("\n\n")
                text_html = (f"{head_part}\n{annotation}{sep}{body}"
                             if sep else f"{text_html}\n{annotation}")

            try:
                sent_ids = sender.send_card(text_html, link=c.link)
            except Exception as e:
                log.warning("raw card push failed (%s): %s", ch.name, e)
                continue
            # write-after-send: a crash re-tries rather than drops. Record EVERY album
            # item, or the incremental high-water mark stalls at the head id.
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            _dedup_register(content_plain, ch, ids, content_store)
            _l2_register(topic_gate, ch, content_plain, sent_ids, sender, l2_verdict)
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
