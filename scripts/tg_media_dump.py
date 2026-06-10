"""Dump a (private) Telegram channel's messages + media for a date window.

Run UNDER the kabi-tg-cli interpreter (it has telethon + the logged-in session):
    <kabi-tg-cli-python> tg_media_dump.py <chat_id> <since> <until> <out_dir> <limit> [min_id]

min_id>0 (incremental mode) fetches the OLDEST page of messages above that id,
ascending, so the caller's high-water mark never advances past an unfetched gap.

since/until are local-tz (Asia/Shanghai) ISO dates: [since, until).
Emits a JSON manifest to stdout: a list (oldest→newest) of
    {msg_id, date, text, grouped_id, media: [{path, kind}]}
where kind ∈ photo|video|audio|document. Media files are downloaded into out_dir.
Web-page link previews and oversized files (>45MB) carry no downloaded media.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import tg_cli.client as tc
from telethon.extensions import html as tg_html

tc._default_api_warned = True  # reuse tgcli config, suppress default-api warning

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
MAX_BYTES = 45 * 1024 * 1024
DOWNLOAD_TIMEOUT = 45  # seconds per media file; a slow/stalled download is skipped
                       # (treated as no-media) so one file can't time out the whole channel


def media_kind(msg) -> str | None:
    if msg.photo:
        return "photo"
    doc = getattr(msg, "document", None)
    if doc:
        mime = (doc.mime_type or "")
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    return None


def media_size(msg) -> int:
    doc = getattr(msg, "document", None)
    if doc and getattr(doc, "size", None):
        return int(doc.size)
    return 0  # photos: unknown/small, allow


async def main() -> int:
    chat_id = int(sys.argv[1])
    since = sys.argv[2]
    until = sys.argv[3]
    out_dir = sys.argv[4]
    limit = int(sys.argv[5])
    min_id = int(sys.argv[6]) if len(sys.argv) > 6 else 0  # 0 = no high-water mark
    os.makedirs(out_dir, exist_ok=True)

    start = datetime.combine(date.fromisoformat(since), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(until), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)

    out: list[dict] = []
    async with tc.connect() as client:
        entity = await client.get_entity(chat_id)
        count = 0
        incremental = min_id > 0
        if incremental:
            # Ascending from the high-water mark (offset_id is exclusive): when more
            # than `limit` messages accumulated since the last run, the budget must be
            # spent on the OLDEST backlog — fetching the newest page would let the
            # caller's seen store advance past the unfetched gap and skip it forever.
            # The remainder is picked up by the next 2-hourly run.
            #
            # Resolve the window-start id first: after a long outage the backlog above
            # the mark may sit entirely BEFORE the window; walking it message-by-message
            # would burn the whole `limit` on skips every run and stall the channel.
            # One 1-message lookup jumps the offset to the window start instead.
            first_in_window = None
            async for m in client.iter_messages(entity, limit=1, reverse=True, offset_date=start):
                first_in_window = m
            if first_in_window is None:
                print(json.dumps([], ensure_ascii=False))
                return 0  # nothing dated >= window start → nothing to forward
            offset = max(min_id, first_in_window.id - 1)
            it = client.iter_messages(entity, limit=limit, reverse=True, offset_id=offset)
        else:
            # offset_date=end → telethon starts at the window's upper bound and walks
            # older, so `limit` is spent on in-window (and older) messages instead of
            # being burned on TODAY's messages that are newer than the window (which
            # would otherwise exhaust the budget before reaching the target day on
            # high-volume channels).
            it = client.iter_messages(entity, limit=limit, offset_date=end)
        async for msg in it:
            md = msg.date  # tz-aware UTC
            if incremental:
                if md < start:
                    continue  # ascending: still before the window, keep walking newer
                if md >= end:
                    break  # ascending: reached the window's upper bound
            elif md >= end:
                continue  # defensive; offset_date should already exclude these
            elif md < start:
                break  # iter is newest→oldest; past the window
            count += 1
            # `html` preserves text-link entities (news source URLs) and bold titles,
            # which the plain `text` loses. Rendering uses `html`; `text` stays for
            # empty/skip checks.
            try:
                html = tg_html.unparse(msg.message or "", msg.entities or [])
            except Exception:
                html = msg.message or ""
            entry = {
                "msg_id": msg.id,
                "date": md.astimezone(LOCAL_TZ).isoformat(),
                "text": (msg.message or ""),
                "html": html,
                "grouped_id": getattr(msg, "grouped_id", None),
                "media": [],
            }
            kind = media_kind(msg)
            if kind and media_size(msg) <= MAX_BYTES:
                try:
                    path = await asyncio.wait_for(
                        msg.download_media(file=os.path.join(out_dir, str(msg.id))),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                    if path:
                        entry["media"].append({"path": path, "kind": kind})
                except Exception as e:  # TimeoutError or download error → skip this file
                    print(f"skip media msg {msg.id}: {type(e).__name__}: {e}", file=sys.stderr)
            out.append(entry)
    if not incremental:
        out.reverse()  # descending iteration → manifest must be oldest → newest
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
