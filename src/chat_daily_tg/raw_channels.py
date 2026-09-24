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
import shutil
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

# Fallback text for a media-only message (empty body) when the real media could
# not be fetched (download failure / no downloadable file). sendMessage text can't
# be empty; the t.me preview behind it renders no image, so this is the last resort.
_MEDIA_PLACEHOLDER = "🖼 （媒体内容，见下方预览 / 原文）"


@dataclass(frozen=True)
class Card:
    text_html: str
    link: str | None


_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s<>]+")
# Inline Markdown supported by the channel-card renderer.  Telegram's Bot API
# receives HTML, while messages cached by tg-cli preserve common Markdown spans.
# Recognising the spans here prevents readers from seeing literal ``**`` / ``~~``
# markers while still escaping every unsupported or malformed construct as text.
_MD_INLINE_RE = re.compile(
    r"(?P<link>\[([^\]\n]+)\]\((https?://[^)\s]+)\))"
    r"|(?P<code>`([^`\n]+)`)"
    r"|(?P<bold>\*\*([^*\n]+?)\*\*)"
    r"|(?P<strike>~~([^~\n]+?)~~)"
    r"|(?P<italic>(?<!\w)_([^_\n]+?)_(?!\w))"
)


def _render_inline_markdown(text: str) -> str:
    """Render the safe, commonly emitted Markdown subset as Telegram HTML.

    All unmatched text is escaped.  This deliberately does not attempt a full
    Markdown implementation: malformed or nested syntax remains readable text,
    rather than risking malformed Telegram HTML and losing the whole card.
    """
    parts: list[str] = []
    pos = 0
    for m in _MD_INLINE_RE.finditer(text):
        parts.append(escape_html(text[pos:m.start()]))
        if m.group("link") is not None:
            label = escape_html(m.group(2))
            href = escape_html(m.group(3)).replace('"', "&quot;")
            parts.append(f'<a href="{href}">{label}</a>')
        elif m.group("code") is not None:
            parts.append(f"<code>{escape_html(m.group(5))}</code>")
        elif m.group("bold") is not None:
            parts.append(f"<b>{escape_html(m.group(7))}</b>")
        elif m.group("strike") is not None:
            parts.append(f"<s>{escape_html(m.group(9))}</s>")
        else:
            parts.append(f"<i>{escape_html(m.group(11))}</i>")
        pos = m.end()
    parts.append(escape_html(text[pos:]))
    return "".join(parts)


def escape_body_html(text: str) -> str:
    """Render safe Markdown spans from a channel body as Telegram HTML.

    Supported spans are links, bold, italic, inline code, and strikethrough.
    Line-oriented Markdown is intentionally left as prose so ordinary channel
    hashtags and punctuation keep their source meaning.
    """
    return "\n".join(_render_inline_markdown(line) for line in text.splitlines())

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



def _same_tick_premerge(
    cards: list,
    ch: RawChannel,
    seen: SeenStore,
    content_store,
    authority: dict | None = None,
    url_authority_skip: bool = False,
) -> list:
    """Within one channel batch, drop later cards that share a text/URL
    fingerprint with an earlier card still pending in this tick.

    Does NOT advance SeenStore for suppressed cards until we intentionally
    terminalize them (same as L1 skip): high-water must not swallow a card
    that merely lost a same-tick race without a durable journal reason.
    First card in batch order wins. Callers should feed cards in source
    timestamp order so chronological first-arrival is preserved.
    """
    if not cards or not ch.dedup:
        return cards
    try:
        from chat_daily_tg.content_seen import fingerprints_for
    except Exception:
        return cards

    claimed: set[str] = set()
    out: list = []
    for ids, c, content_plain in cards:
        if not content_plain:
            out.append((ids, c, content_plain))
            continue
        try:
            fps = fingerprints_for(content_plain)
        except Exception:
            out.append((ids, c, content_plain))
            continue
        if fps and any(fp in claimed for fp in fps):
            log.info("skip same-tick content-dup (%s msg %s)", ch.name, ids[0])
            try:
                from chat_daily_tg import dedup_journal
                dedup_journal.record({
                    "layer": "L1", "action": "skip", "reason": "same_tick",
                    "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
                    "text_head": content_plain[:120],
                })
            except Exception:
                pass
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            continue
        for fp in fps:
            claimed.add(fp)
        out.append((ids, c, content_plain))
    return out


def _dedup_skip(content_plain: str, ch: RawChannel, ids: list[int],
                seen: SeenStore, content_store,
                authority: dict | None = None,
                url_authority_skip: bool = False,
                xmon=None) -> bool:
    """L1 content-dedup gate. True = suppress this card (already journaled and
    marked seen). Any internal failure returns False — dedup must never block
    delivery (投递优先于完美)."""
    if content_store is None or not ch.dedup:
        return False
    try:
        from chat_daily_tg.content_seen import check_duplicate
        d = check_duplicate(
            content_plain, store=content_store, xmon=xmon,
            channel_name=ch.name, channel_id=str(ch.id),
            authority=authority, url_authority_skip=url_authority_skip,
        )
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
        content_store.register_title(content_plain, ch.id, ids[0], ch.name)
    except Exception as e:
        log.warning("content dedup register failed (%s msg %s): %s", ch.name, ids[0], e)


def _media_dedup_skip(paths, ch: RawChannel, ids: list[int],
                     seen: SeenStore, content_store) -> bool:
    """True when identical media bytes were already delivered."""
    if content_store is None or not ch.dedup or not paths:
        return False
    try:
        from chat_daily_tg.content_seen import check_media_duplicate
        d = check_media_duplicate(paths, store=content_store)
        if not d.skip:
            return False
        log.info("skip media-dup (%s msg %s): %s hit ← %s msg %s @ %s",
                 ch.name, ids[0], d.reason,
                 d.detail.get("matched_channel", "?"),
                 d.detail.get("matched_msg_id", "?"),
                 d.detail.get("matched_sent_at", "?"))
        try:
            from chat_daily_tg import dedup_journal
            dedup_journal.record({
                "layer": "L1", "action": "skip", "reason": d.reason,
                "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
                **d.detail,
            })
        except Exception:
            pass
        for mid in ids:
            seen.add(SeenStore.key(ch.id, mid))
        return True
    except Exception as e:
        log.warning("media dedup check failed (%s msg %s), delivering: %s",
                    ch.name, ids[0], e)
        return False


def _media_dedup_register(paths, ch: RawChannel, ids: list[int], content_store) -> None:
    if content_store is None or not ch.dedup or not paths:
        return
    try:
        from chat_daily_tg.content_seen import media_fingerprints_for
        content_store.register(media_fingerprints_for(paths), ch.id, ids[0], ch.name)
    except Exception as e:
        log.warning("media dedup register failed (%s msg %s): %s", ch.name, ids[0], e)


def _terminalize_ambiguous_delivery(
    exc: Exception, ch: RawChannel, ids: list[int], seen: SeenStore,
) -> bool:
    """Persist an unknown Bot API outcome so it is never blindly replayed.

    Telegram's Bot API has no idempotency key.  A read/write timeout can mean
    "message accepted, response lost" (the 2026-08-02 triple-photo incident).
    Marking every source member terminal prevents both an in-process retry and
    the next incremental run from multiplying a likely-delivered post.  The
    journal plus alert preserve an explicit recovery trail for the rarer case
    where the request did not land; ``channels resend`` remains the manual
    recovery hatch for public text cards.
    """
    from chat_daily_tg.tg_sender import AmbiguousDeliveryError
    if not isinstance(exc, AmbiguousDeliveryError):
        return False
    for mid in ids:
        seen.add(SeenStore.key(ch.id, mid))
    try:
        from chat_daily_tg import dedup_journal
        dedup_journal.record({
            "layer": "delivery", "action": "ambiguous",
            "reason": "telegram_transport_timeout", "method": exc.method,
            "chat_id": ch.id, "msg_id": ids[0], "member_ids": ids,
            "channel": ch.name,
        })
    except Exception:
        pass
    log.error("ambiguous delivery terminalized (%s msg %s via %s); automatic replay suppressed",
              ch.name, ids[0], exc.method)
    try:
        from chat_daily_tg.notifier import notify_failure
        notify_failure(
            "chat-daily-tg 投递结果待确认",
            f"{ch.name} msg {ids[0]} 的 {exc.method} 响应超时；已停止自动重试以避免重复，"
            "请核对目标话题，若确实缺失再手动补发。",
        )
    except Exception:
        pass
    return True


def _l2_check(topic_gate, ch: RawChannel, content_plain: str, ids: list[int],
              seen: SeenStore, *, has_media: bool = False) -> tuple[bool, str, object]:
    """L2 topic-gate decision, shared by the public and private send paths.
    Returns (skip, annotation_html, verdict). skip=True means the card was
    journaled (with its own chat_id:msg_id for --resend) and marked seen.
    Any failure returns (False, "", None) — deliver."""
    if topic_gate is None or not ch.dedup:
        return False, "", None
    try:
        v = topic_gate.assess(content_plain, ref={
            "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
            "producer": "chatdaily_raw", "has_media": has_media,
        })
        if v.action == "skip" and not has_media:
            log.info("skip topic-dup (%s msg %s): sim=%.2f vs msg %s",
                     ch.name, ids[0], v.similarity, v.matched_msg_id)
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            return True, "", v
        if v.action in ("annotate", "skip") and v.matched_msg_id:
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


@dataclass
class _PendingPublic:
    """One public-channel card waiting for chronological cross-channel send."""
    ch: RawChannel
    ids: list[int]
    card: Card
    content_plain: str
    ts: float  # unix seconds; missing/bad timestamps sort last within the run


def _row_ts(row) -> float:
    try:
        return parse_timestamp(row["timestamp"]).timestamp()
    except Exception:
        return float("inf")


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
    authority: dict | None = None,
    url_authority_skip: bool = False,
    xmon=None,            # content_seen.XMonitorIndex | None
    sent_content_ledger_path: str | Path | None = None,
) -> int:
    """Export each raw channel's window and push every message as a card.

    Public channels are exported first, then sent in source-timestamp order so
    cross-channel collisions resolve by chronological first-arrival rather than
    config order. Private channels stay on the media-download path and run after
    public cards of the same call (their posts are already oldest→newest).

    Returns the number of cards pushed. A failure on a single channel/message is
    logged and skipped; it never aborts the remaining channels. Already-pushed
    message ids (tracked in `seen_path`) are skipped, so re-runs/retries don't
    duplicate. incremental=True fetches only messages newer than each channel's
    high-water mark.
    """
    seen = SeenStore(seen_path)
    total = 0
    private_attempted = 0
    private_failed = 0
    pending: list[_PendingPublic] = []
    media_dirs: list[Path] = []

    for ch in channels:
        hwm = seen.max_msg_id(ch.id) if incremental else 0
        # Private channels (no public username) get the media-download path.
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
                    authority=authority, url_authority_skip=url_authority_skip,
                    xmon=xmon,
                )
            except Exception as e:
                private_failed += 1
                log.warning("private channel push failed for %s: %s", ch.name, e)
            continue

        try:
            if sync_before_export:
                sync_chat(
                    ch.id, limit=ch.limit, db_path=db_path,
                    min_msg_id=hwm,
                )
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

        cards: list[tuple[list[int], Card, str, float]] = []
        excluded_ids: list[int] = []
        excluded_posts: list[tuple[int, str]] = []
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
                cards.append((ids, c, content_plain, _row_ts(head)))
        log.info("raw channel %s: %d msgs → %d cards (%d filtered)",
                 ch.name, len(rows), len(cards), len(excluded_ids))

        archive_path = Path(archive_dir) / f"rawcard-{_safe(ch.name)}.md"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(
            "\n\n---\n\n".join(
                (c.text_html + (f"\n\n[原文] {c.link}" if c.link else ""))
                for _, c, _, _ in cards
            )
            or "(无消息)",
            encoding="utf-8",
        )

        # Exclusions are terminal regardless of --no-push so incremental HWM moves.
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

        if no_push:
            continue

        for ids, c, content_plain, ts in cards:
            pending.append(_PendingPublic(ch, ids, c, content_plain, ts))

    if no_push:
        return total

    # Chronological first-arrival across public channels in this topic batch.
    pending.sort(key=lambda p: (p.ts, str(p.ch.id), p.ids[0]))

    # Cross-channel same-tick premerge on the sorted stream.
    if content_store is not None and pending:
        try:
            from chat_daily_tg.content_seen import fingerprints_for
            claimed: set[str] = set()
            kept: list[_PendingPublic] = []
            for item in pending:
                if not item.ch.dedup or not item.content_plain:
                    kept.append(item)
                    continue
                try:
                    fps = fingerprints_for(item.content_plain)
                except Exception:
                    kept.append(item)
                    continue
                if fps and any(fp in claimed for fp in fps):
                    log.info("skip same-tick content-dup (%s msg %s)",
                             item.ch.name, item.ids[0])
                    try:
                        from chat_daily_tg import dedup_journal
                        dedup_journal.record({
                            "layer": "L1", "action": "skip", "reason": "same_tick",
                            "chat_id": item.ch.id, "msg_id": item.ids[0],
                            "channel": item.ch.name,
                            "text_head": item.content_plain[:120],
                        })
                    except Exception:
                        pass
                    for mid in item.ids:
                        seen.add(SeenStore.key(item.ch.id, mid))
                    continue
                for fp in fps:
                    claimed.add(fp)
                kept.append(item)
            pending = kept
        except Exception as e:
            log.warning("cross-channel same-tick premerge failed: %s", e)

    # Batch media downloads per channel for empty-body cards still pending.
    media_map: dict[tuple[str, int], list[tuple[str, str]]] = {}
    media_caption_map: dict[tuple[str, int], tuple[str, str]] = {}
    media_only_by_ch: dict[str, list[int]] = {}
    for item in pending:
        if item.content_plain:
            continue
        if SeenStore.key(item.ch.id, item.ids[0]) in seen:
            continue
        media_only_by_ch.setdefault(item.ch.id, [])
        media_only_by_ch[item.ch.id].extend(item.ids)
    ch_by_id = {ch.id: ch for ch in channels}
    for chat_id, mids in media_only_by_ch.items():
        ch = ch_by_id.get(chat_id)
        if ch is None:
            continue
        media_dir = Path(archive_dir) / f"rawmedia-{_safe(ch.name)}"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dirs.append(media_dir)
        try:
            from chat_daily_tg.private_media import dump_messages_by_ids, media_by_msg_id
            manifest = dump_messages_by_ids(ch.id, mids, media_dir)
            by_mid = media_by_msg_id(manifest)
            for mid, items in by_mid.items():
                media_map[(ch.id, mid)] = items
            # The tg-cli database may omit captions for media messages. The
            # targeted Telethon manifest is the authoritative fallback for this
            # one field; keep the caption separate from the binary media map.
            for entry in manifest:
                text = str(entry.get("text") or "").strip()
                html = str(entry.get("html") or escape_html(text)).strip()
                if text:
                    media_caption_map[(ch.id, int(entry["msg_id"]))] = (text, html)
        except Exception as e:
            log.warning("raw media fetch failed for %s (placeholder fallback): %s",
                        ch.name, e)

    if topic_gate is not None and pending:
        try:
            unseen = []
            for item in pending:
                if not item.ch.dedup or SeenStore.key(item.ch.id, item.ids[0]) in seen:
                    continue
                text = item.content_plain or next(
                    (media_caption_map[(item.ch.id, mid)][0] for mid in item.ids
                     if (item.ch.id, mid) in media_caption_map), "")
                text = strip_promo_lines(text, item.ch.strip_patterns)
                if text and not matches_exclude_patterns(text, item.ch.exclude_patterns):
                    unseen.append(text)
            if unseen:
                topic_gate.prepare(unseen)
        except Exception as e:
            log.warning("topic gate prepare failed (public batch): %s", e)

    try:
        for item in pending:
            ch, ids, c, content_plain = item.ch, item.ids, item.card, item.content_plain
            if SeenStore.key(ch.id, ids[0]) in seen:
                continue

            media = [m for mid in ids for m in media_map.get((ch.id, mid), [])]
            downloaded_caption = next(
                (media_caption_map[(ch.id, mid)] for mid in ids
                 if (ch.id, mid) in media_caption_map), None)
            if downloaded_caption:
                downloaded_text, downloaded_html = downloaded_caption
                content_plain = strip_promo_lines(downloaded_text, ch.strip_patterns)
                header = c.text_html.partition("\n\n")[0]
                rendered = strip_promo_lines_html(downloaded_html, ch.strip_patterns)
                c = Card(text_html=f"{header}\n\n{rendered}" if rendered else header,
                         link=c.link)
                if matches_exclude_patterns(downloaded_text, ch.exclude_patterns):
                    try:
                        from chat_daily_tg import dedup_journal
                        journaled = dedup_journal.record({
                            "layer": "L1", "action": "skip", "reason": "exclude_pattern",
                            "chat_id": ch.id, "msg_id": ids[0], "channel": ch.name,
                            "text_head": downloaded_text[:120],
                        })
                        if journaled is False:
                            raise OSError("caption exclusion was not journaled")
                    except Exception as exc:
                        log.warning("caption exclusion journal failed: %s", exc)
                    else:
                        for mid in ids:
                            seen.add(SeenStore.key(ch.id, mid))
                        continue
            if media:
                media_paths = [p for p, _ in media]
                if _media_dedup_skip(media_paths, ch, ids, seen, content_store):
                    continue
                l2_verdict = None
                annotation = ""
                if content_plain:
                    l2_skip, annotation, l2_verdict = _l2_check(
                        topic_gate, ch, content_plain, ids, seen, has_media=True)
                    if l2_skip:
                        continue
                caption = c.text_html.partition("\n\n")[0]
                if downloaded_caption:
                    rendered = strip_promo_lines_html(downloaded_html, ch.strip_patterns)
                    caption = f"{caption}\n\n{rendered}" if rendered else caption
                if annotation:
                    caption = f"{caption}\n{annotation}"
                if c.link:
                    caption = f'{caption} · <a href="{escape_html(c.link)}">原文</a>'
                try:
                    from chat_daily_tg.private_media import _send_media
                    sent_ids = []
                    if len(visible_text(caption)) <= 1024:
                        dropped = _send_media(media, sender, caption, sent_ids=sent_ids)
                    else:
                        # Only the text card contains the full caption. Index its
                        # IDs so future annotations link to the visible evidence.
                        dropped = _send_media(media, sender, "")
                        sent_ids = sender.send_card(caption, link=c.link) or []
                except Exception as e:
                    if _terminalize_ambiguous_delivery(e, ch, ids, seen):
                        continue
                    for mid in ids:
                        seen.add_hole(ch.id, mid)
                    log.warning("raw media push failed (%s msg %s), "
                                "placeholder fallback: %s", ch.name, ids[0], e)
                else:
                    if dropped:
                        log.warning("raw media partial loss (%s msg %s): "
                                    "%d item(s) not sent", ch.name, ids[0], dropped)
                    for mid in ids:
                        seen.add(SeenStore.key(ch.id, mid))
                    _media_dedup_register(media_paths, ch, ids, content_store)
                    if content_plain:
                        _l2_register(topic_gate, ch, content_plain, sent_ids,
                                     sender, l2_verdict)
                    total += 1
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                    continue
                # Media send failed → fall through to placeholder card path.

            if content_plain and not (media or downloaded_caption) and _dedup_skip(
                    content_plain, ch, ids, seen, content_store,
                    authority=authority, url_authority_skip=url_authority_skip,
                    xmon=xmon):
                continue

            l2_verdict = None
            annotation = ""
            if content_plain:
                l2_skip, annotation, l2_verdict = _l2_check(
                    topic_gate, ch, content_plain, ids, seen,
                    has_media=bool(media or downloaded_caption))
                if l2_skip:
                    continue
            text_html = c.text_html
            if annotation:
                head_part, sep, body = text_html.partition("\n\n")
                text_html = (f"{head_part}\n{annotation}{sep}{body}"
                             if sep else f"{text_html}\n{annotation}")

            try:
                sent_ids = sender.send_card(text_html, link=c.link)
            except Exception as e:
                if _terminalize_ambiguous_delivery(e, ch, ids, seen):
                    continue
                for mid in ids:
                    seen.add_hole(ch.id, mid)
                log.warning("raw card push failed (%s): %s", ch.name, e)
                continue
            for mid in ids:
                seen.add(SeenStore.key(ch.id, mid))
            # General content provenance is independent of the r4s-owned media
            # ledger.  Record only successful public text-card sends; media and
            # private paths intentionally retain their existing handoff rules.
            # Seen is advanced first to preserve the existing no-replay
            # semantics even when this best-effort append fails.
            if content_plain and sent_ids:
                try:
                    from chat_daily_tg.sent_content_ledger import append_message_ids

                    username = (ch.username or "").lstrip("@")
                    source_ref = f"https://t.me/{username}/{ids[0]}"
                    ledger_path = sent_content_ledger_path
                    if ledger_path is None:
                        # Keep alternate runtime roots (including tests) self-
                        # contained while resolving to the standard state path
                        # for production's DATA_DIR/raw_channel_seen.txt.
                        from chat_daily_tg.paths import SENT_CONTENT_LEDGER
                        ledger_path = (
                            Path(seen_path).expanduser().parent
                            / "state" / SENT_CONTENT_LEDGER.name
                        )
                    append_message_ids(
                        sent_ids,
                        chat_id=getattr(sender, "chat_id", ""),
                        thread_id=getattr(sender, "message_thread_id", None),
                        producer="chatdaily_raw",
                        source_kind="telegram_channel",
                        source_ref=source_ref,
                        source_message_ids=ids,
                        url=c.link or source_ref,
                        content=content_plain,
                        content_id=f"telegram-channel:{ch.id}:{ids[0]}",
                        path=ledger_path,
                    )
                except Exception as e:
                    log.warning("sent-content ledger write failed (%s msg %s): %s",
                                ch.name, ids[0], e)
            if content_plain:
                _dedup_register(content_plain, ch, ids, content_store)
                _l2_register(topic_gate, ch, content_plain, sent_ids, sender, l2_verdict)
            total += 1
            if delay_seconds > 0:
                time.sleep(delay_seconds)
    finally:
        for media_dir in media_dirs:
            shutil.rmtree(media_dir, ignore_errors=True)

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
