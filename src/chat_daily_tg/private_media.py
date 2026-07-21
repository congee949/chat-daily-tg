"""Private-channel verbatim push WITH media.

Private channels have no public t.me/<username> link, so Telegram cannot render a
preview card. Instead we download each message's media via the logged-in user session
(kabi-tg-cli's telethon, invoked as a subprocess) and re-upload it through the bot,
together with the verbatim text and a clickable 打开原文 link (t.me/c/<internal>/<id>,
which opens in-app for the member). Albums are sent as media groups.

The downloader runs under the kabi-tg-cli interpreter (it owns telethon + the session);
this module only orchestrates and sends, keeping chat-daily-tg's venv telethon-free.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from chat_daily_tg.config import RawChannel
from chat_daily_tg.notifier import notify_failure
from chat_daily_tg.raw_channels import (
    _dedup_register,
    _dedup_skip,
    _l2_check,
    _l2_register,
    matches_exclude_patterns,
    strip_promo_lines,
    strip_promo_lines_html,
    visible_text,
)
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.tg_sender import TelegramSender, escape_html

log = logging.getLogger(__name__)

# kabi-tg-cli interpreter (has telethon + the logged-in session). Overridable for tests.
TG_CLI_PYTHON = os.environ.get(
    "CHAT_DAILY_TG_CLI_PYTHON",
    os.path.expanduser("~/.local/share/uv/tools/kabi-tg-cli/bin/python"),
)
_DUMP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tg_media_dump.py"


@dataclass
class Post:
    """One logical post = a standalone message or a grouped album."""
    first_msg_id: int
    time: str  # HH:MM local
    text: str          # plain text (for empty/skip checks)
    html: str = ""     # Telegram HTML (preserves source links + bold) for rendering
    media: list[tuple[str, str]] = field(default_factory=list)  # (path, kind)
    # ALL message ids folded into this post (album items share one Post). Every id
    # must land in the seen store, or the high-water mark stalls at the album head.
    msg_ids: list[int] = field(default_factory=list)


def _require_tg_cli() -> None:
    """Fail fast with a clear cause when the kabi-tg-cli interpreter is gone (uv
    prune/upgrade can break this path the same way it broke .venv). Without
    this, every media dump dies with an opaque errno buried in stderr
    (review finding #20)."""
    if not os.access(TG_CLI_PYTHON, os.X_OK):
        raise RuntimeError(
            f"kabi-tg-cli python not executable at {TG_CLI_PYTHON} — reinstall with "
            "`uv tool install kabi-tg-cli` or set CHAT_DAILY_TG_CLI_PYTHON"
        )


def dump_channel(chat_id: str, since: str, until: str, out_dir: Path, limit: int,
                 min_id: int = 0) -> list[dict]:
    """Run the telethon downloader and return its message manifest (oldest→newest).
    min_id>0 fetches only messages newer than that id (incremental forwarder)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _require_tg_cli()
    cmd = [TG_CLI_PYTHON, str(_DUMP_SCRIPT), str(chat_id), since, until, str(out_dir),
           str(limit), str(min_id)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"tg_media_dump failed for {chat_id}: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout or "[]")


def dump_messages_by_ids(chat_id: str, msg_ids: list[int], out_dir: Path) -> list[dict]:
    """Targeted variant of dump_channel: fetch EXACTLY these message ids (any date).
    The public-channel media-only path knows the ids from messages.db and only
    needs their media. since/until are dummies — the script ignores the window
    when only_ids is set."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _require_tg_cli()
    cmd = [TG_CLI_PYTHON, str(_DUMP_SCRIPT), str(chat_id), "2000-01-01", "2100-01-01",
           str(out_dir), str(len(msg_ids)), "0", ",".join(str(i) for i in msg_ids)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"tg_media_dump ids failed for {chat_id}: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout or "[]")


def media_by_msg_id(manifest: list[dict]) -> dict[int, list[tuple[str, str]]]:
    """{msg_id: [(path, kind), …]} for manifest entries that carry downloaded media."""
    return {e["msg_id"]: [(m["path"], m["kind"]) for m in e.get("media", [])]
            for e in manifest if e.get("media")}


def group_posts(manifest: list[dict]) -> list[Post]:
    """Collapse album items (same grouped_id) into one Post; keep order."""
    posts: list[Post] = []
    by_group: dict[int, Post] = {}
    for e in manifest:
        gid = e.get("grouped_id")
        media = [(m["path"], m["kind"]) for m in e.get("media", [])]
        text = (e.get("text") or "").strip()
        html = (e.get("html") or "").strip()
        if gid is not None and gid in by_group:
            p = by_group[gid]
            p.media.extend(media)
            p.msg_ids.append(e["msg_id"])
            if text and not p.text:
                p.text = text
                p.html = html
            continue
        p = Post(first_msg_id=e["msg_id"], time=e["date"][11:16], text=text, html=html, media=media,
                 msg_ids=[e["msg_id"]])
        posts.append(p)
        if gid is not None:
            by_group[gid] = p
    return posts


def _send_media(post_media: list[tuple[str, str]], sender: TelegramSender, caption: str) -> int:
    """Send a post's media; returns the count of items that FAILED to send (0 = all sent).

    single → send_media; all-visual album → media group; mixed types → individual
    sends (caption on the first SUCCESSFUL item — pinning it to item 0 would
    silently drop the verbatim text whenever item 0 fails but a later item
    succeeds). A per-item failure is tolerated (logged) so one bad item doesn't
    unwind the whole post; it raises only if EVERY item failed (nothing delivered).
    A non-zero return lets the caller surface the partial loss (review finding #13)."""
    if len(post_media) == 1:
        path, kind = post_media[0]
        sender.send_media(path, kind, caption=caption)
        return 0
    visual = all(k in ("photo", "video") for _, k in post_media)
    if visual:
        sender.send_media_group(post_media, caption=caption)
        return 0
    ok = 0
    caption_pending = bool(caption)
    last_exc: Exception | None = None
    for path, kind in post_media:
        try:
            sender.send_media(path, kind, caption=caption if caption_pending else "")
            ok += 1
            caption_pending = False
        except Exception as e:
            last_exc = e
            log.warning("mixed-album item failed (%s): %s", path, e)
    if ok == 0 and last_exc is not None:
        raise last_exc
    return len(post_media) - ok


def push_private_channel(
    *,
    channel: RawChannel,
    since: str,
    until: str,
    out_dir: Path,
    sender: TelegramSender | None,
    limit: int,
    seen: "SeenStore | None" = None,
    min_id: int = 0,
    delay_seconds: float = 1.0,
    no_push: bool = False,
    content_store=None,   # content_seen.ContentSeenStore | None (L1 dedup)
    topic_gate=None,      # topic_dedup.TopicDedupGate | None (L2 dedup)
) -> int:
    """Download + push one private channel's window. Returns posts pushed.

    --no-push short-circuits BEFORE the (expensive) telethon download so a dry run
    stays cheap. min_id>0 only downloads messages newer than that id (incremental).
    Already-pushed posts (tracked in `seen`) are skipped. Downloaded media is deleted
    after the channel finishes to avoid unbounded disk growth."""
    if no_push:
        return 0  # skip the heavy media download entirely on a dry run

    import shutil
    import time as _t
    manifest = dump_channel(channel.id, since, until, out_dir, limit, min_id)
    posts = group_posts(manifest)
    log.info("private channel %s: %d msgs → %d posts (with media)",
             channel.name, len(manifest), len(posts))

    pushed = 0
    dropped_posts: list[tuple[int, int]] = []  # (first_msg_id, dropped_count)
    if topic_gate is not None and channel.dedup and posts:
        try:  # one embed batch per channel (same as the public path); failure →
            # the gate falls back to per-card lazy embedding or goes offline.
            # Only posts that will actually be assessed: unseen, not matching an
            # exclude pattern (paying embedding quota for configured spam was
            # review finding E4), and text-only (media posts bypass the gates).
            topic_gate.prepare([
                strip_promo_lines(p.text or "", channel.strip_patterns)
                for p in posts
                if not p.media
                and (seen is None or SeenStore.key(channel.id, p.first_msg_id) not in seen)
                and not matches_exclude_patterns(p.text, channel.exclude_patterns)
            ])
        except Exception as e:
            log.warning("topic gate prepare failed (%s): %s", channel.name, e)
    try:
        for p in posts:
            key = SeenStore.key(channel.id, p.first_msg_id) if seen is not None else None
            if key is not None and key in seen:
                continue
            if matches_exclude_patterns(p.text, channel.exclude_patterns):
                if seen is not None:
                    for mid in (p.msg_ids or [p.first_msg_id]):
                        seen.add(SeenStore.key(channel.id, mid))
                try:
                    from chat_daily_tg import dedup_journal
                    dedup_journal.record({
                        "layer": "L1", "action": "skip", "reason": "exclude_pattern",
                        "chat_id": channel.id, "msg_id": p.first_msg_id,
                        "channel": channel.name, "text_head": (p.text or "")[:120],
                    })
                except Exception:
                    pass
                continue
            # Dedup layers operate on the promo-stripped PLAIN text (no header/HTML —
            # those differ per channel and would defeat cross-channel matching).
            content_plain = strip_promo_lines(p.text or "", channel.strip_patterns)
            member_ids = p.msg_ids or [p.first_msg_id]
            l2_verdict = None
            annotation = ""
            if not p.media:
                # Suppression applies to TEXT posts only. A media post's caption
                # is not its content: an album captioned with a bare URL that a
                # text card already delivered still carries photos nobody has
                # seen — skipping it would terminally lose them (宁可重复不可误杀,
                # review finding A4). Media posts bypass both gates entirely.
                if seen is not None and _dedup_skip(
                        content_plain, channel, member_ids, seen, content_store):
                    continue
                l2_skip, annotation, l2_verdict = _l2_check(
                    topic_gate, channel, content_plain, member_ids,
                    seen) if seen is not None else (False, "", None)
                if l2_skip:
                    continue
            # Use the message HTML (keeps news-source links + bold), drop promo lines.
            content_html = strip_promo_lines_html(p.html or escape_html(p.text), channel.strip_patterns)
            has_text = bool(visible_text(content_html).strip())
            header = f"📢 <b>{escape_html(channel.name)}</b> · {p.time}"
            if annotation:
                # Inserted BEFORE the 1024-caption check below so the overflow
                # fallback accounts for the extra line.
                header = f"{header}\n{annotation}"
            body = f"{header}\n\n{content_html}" if has_text else header
            dropped = 0
            sent_ids: list[int] = []
            try:
                if p.media:
                    # Merge text + media into ONE message: the text rides as the media
                    # caption. Telegram caps captions at 1024 VISIBLE chars (HTML tags
                    # don't count) — only when that overflows do we fall back to a
                    # separate text message + bare media.
                    if len(visible_text(body)) <= 1024:
                        dropped = _send_media(p.media, sender, caption=body)
                    else:
                        sender.send_card(body, link=None)
                        dropped = _send_media(p.media, sender, caption="")
                elif has_text:
                    sent_ids = sender.send_card(body, link=None) or []
                else:
                    continue  # nothing left after stripping promo lines, no media
            except Exception as e:
                log.warning("private post push failed (%s msg %s): %s", channel.name, p.first_msg_id, e)
                continue
            if dropped:
                # The incremental high-water mark is a max over seen ids, so a later
                # fully-sent post advances it past this one regardless — withholding
                # `seen` here wouldn't actually trigger a retry. So mark seen as usual
                # and collect the loss for ONE aggregated alert per channel below,
                # turning a silent media loss into a visible one (review finding #13).
                dropped_posts.append((p.first_msg_id, dropped))
            if key is not None:
                # Write-after-send, and record EVERY album item id — recording only
                # the first would stall max_msg_id at the album head, making the next
                # incremental run re-fetch and re-send the tail as a partial album.
                for mid in (p.msg_ids or [p.first_msg_id]):
                    seen.add(SeenStore.key(channel.id, mid))
            _dedup_register(content_plain, channel, member_ids, content_store)
            # Text-only sends surface their forum message ids and register into
            # the delivered index directly; media sends don't (the next forum
            # sync ingests the delivered copy instead).
            _l2_register(topic_gate, channel, content_plain, sent_ids, sender, l2_verdict)
            pushed += 1
            if delay_seconds > 0:
                _t.sleep(delay_seconds)
        if dropped_posts:
            total = sum(d for _, d in dropped_posts)
            ids = ", ".join(str(mid) for mid, _ in dropped_posts)
            notify_failure(
                "chat-daily-tg 私有频道媒体丢失",
                f"{channel.name}: {total} 个媒体未发出（{len(dropped_posts)} 帖：msg {ids}；"
                "文本已送达，需手动补），见日志。",
            )
    finally:
        # The verbatim text is already delivered; the heavy media binaries are
        # re-downloadable next run, so don't let them accumulate in the archive tree.
        shutil.rmtree(out_dir, ignore_errors=True)
    return pushed
