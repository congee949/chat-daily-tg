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
from chat_daily_tg.raw_channels import strip_promo_lines_html, visible_text
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


def dump_channel(chat_id: str, since: str, until: str, out_dir: Path, limit: int,
                 min_id: int = 0) -> list[dict]:
    """Run the telethon downloader and return its message manifest (oldest→newest).
    min_id>0 fetches only messages newer than that id (incremental forwarder)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [TG_CLI_PYTHON, str(_DUMP_SCRIPT), str(chat_id), since, until, str(out_dir),
           str(limit), str(min_id)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"tg_media_dump failed for {chat_id}: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout or "[]")


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


def _send_media(post_media: list[tuple[str, str]], sender: TelegramSender, caption: str) -> None:
    """Send a post's media: single → send_media; all-visual album → media group;
    mixed types → fall back to individual sends (caption on the first SUCCESSFUL
    item — pinning it to item 0 would silently drop the verbatim text whenever
    item 0 fails but a later item succeeds, because the post is then marked seen).
    A per-item failure is tolerated (logged) so one bad item doesn't unwind the
    whole post; it only raises if EVERY item failed."""
    if len(post_media) == 1:
        path, kind = post_media[0]
        sender.send_media(path, kind, caption=caption)
        return
    visual = all(k in ("photo", "video") for _, k in post_media)
    if visual:
        sender.send_media_group(post_media, caption=caption)
        return
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
    try:
        for p in posts:
            key = SeenStore.key(channel.id, p.first_msg_id) if seen is not None else None
            if key is not None and key in seen:
                continue
            # Use the message HTML (keeps news-source links + bold), drop promo lines.
            content_html = strip_promo_lines_html(p.html or escape_html(p.text), channel.strip_patterns)
            has_text = bool(visible_text(content_html).strip())
            header = f"📢 <b>{escape_html(channel.name)}</b> · {p.time}"
            body = f"{header}\n\n{content_html}" if has_text else header
            try:
                if p.media:
                    # Merge text + media into ONE message: the text rides as the media
                    # caption. Telegram caps captions at 1024 VISIBLE chars (HTML tags
                    # don't count) — only when that overflows do we fall back to a
                    # separate text message + bare media.
                    if len(visible_text(body)) <= 1024:
                        _send_media(p.media, sender, caption=body)
                    else:
                        sender.send_card(body, link=None)
                        _send_media(p.media, sender, caption="")
                elif has_text:
                    sender.send_card(body, link=None)
                else:
                    continue  # nothing left after stripping promo lines, no media
            except Exception as e:
                log.warning("private post push failed (%s msg %s): %s", channel.name, p.first_msg_id, e)
                continue
            if key is not None:
                # Write-after-send, and record EVERY album item id — recording only
                # the first would stall max_msg_id at the album head, making the next
                # incremental run re-fetch and re-send the tail as a partial album.
                for mid in (p.msg_ids or [p.first_msg_id]):
                    seen.add(SeenStore.key(channel.id, mid))
            pushed += 1
            if delay_seconds > 0:
                _t.sleep(delay_seconds)
    finally:
        # The verbatim text is already delivered; the heavy media binaries are
        # re-downloadable next run, so don't let them accumulate in the archive tree.
        shutil.rmtree(out_dir, ignore_errors=True)
    return pushed
