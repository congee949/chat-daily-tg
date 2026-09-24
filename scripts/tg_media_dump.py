"""Dump a (private) Telegram channel's messages + media for a date window.

Run UNDER the kabi-tg-cli interpreter (it has telethon + the logged-in session):
    <kabi-tg-cli-python> tg_media_dump.py <chat_id> <since> <until> <out_dir> <limit> [min_id] [only_ids] [download_kinds] [max_downloads]

Daily-summary batch mode reads one JSON payload from stdin and reuses one
Telethon connection while processing chats serially:
    <kabi-tg-cli-python> tg_media_dump.py --batch-json

min_id>0 (incremental mode) fetches the OLDEST page of messages above that id,
ascending and without a rolling lower date bound, so an outage backlog is not
lost after it slides outside the normal date window.

only_ids (optional 7th arg, comma-separated msg ids) fetches EXACTLY those
messages, ignoring the date window entirely — the public-channel media-only path
already knows the ids from messages.db and only needs their media.

download_kinds (optional 8th arg, comma-separated) and max_downloads (optional
9th arg) constrain which selected messages perform media downloads. The manifest
still contains every selected message, preserving private-channel semantics;
these knobs only avoid downloading media the caller will discard.

since/until are local-tz (Asia/Shanghai) ISO dates: [since, until).
Emits a JSON manifest to stdout: a list (oldest→newest) of
    {msg_id, date, text, grouped_id, media: [{path, kind}]}
where kind ∈ photo|video|audio|document. Media files are downloaded into out_dir
with bounded concurrency (CHAT_DAILY_MEDIA_CONCURRENCY, default 4); the manifest
order stays the message order regardless of download completion order.
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
DEFAULT_DOWNLOAD_CONCURRENCY = 4
MEDIA_KINDS = frozenset({"photo", "video", "audio", "document"})


def download_concurrency() -> int:
    raw = os.environ.get("CHAT_DAILY_MEDIA_CONCURRENCY", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DOWNLOAD_CONCURRENCY
    return value if value > 0 else DEFAULT_DOWNLOAD_CONCURRENCY


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


async def entry_for(msg, out_dir: str, *, download_media: bool = True) -> dict:
    """Build one manifest entry, downloading the media when there is any.
    `html` preserves text-link entities (news source URLs) and bold titles,
    which the plain `text` loses. Rendering uses `html`; `text` stays for
    empty/skip checks."""
    try:
        html = tg_html.unparse(msg.message or "", msg.entities or [])
    except Exception:
        html = msg.message or ""
    entry = {
        "msg_id": msg.id,
        "date": msg.date.astimezone(LOCAL_TZ).isoformat(),
        "text": (msg.message or ""),
        "html": html,
        "grouped_id": getattr(msg, "grouped_id", None),
        "media": [],
    }
    kind = media_kind(msg)
    if download_media and kind and media_size(msg) <= MAX_BYTES:
        try:
            path = await asyncio.wait_for(
                msg.download_media(file=os.path.join(out_dir, str(msg.id))),
                timeout=DOWNLOAD_TIMEOUT,
            )
            if path:
                entry["media"].append({"path": path, "kind": kind})
        except Exception as e:  # TimeoutError or download error → skip this file
            print(f"skip media msg {msg.id}: {type(e).__name__}: {e}", file=sys.stderr)
    return entry


async def entries_for(msgs: list, out_dir: str, *, download_ids: set[int] | None = None) -> list[dict]:
    """Build manifest entries for msgs, downloading media with bounded concurrency
    (telethon supports concurrent download_media on one client/event loop). The
    returned list mirrors the input message order — NOT completion order — and a
    failed/timed-out file still only skips that one entry's media."""
    semaphore = asyncio.Semaphore(download_concurrency())

    async def bounded(msg) -> dict:
        async with semaphore:
            return await entry_for(
                msg, out_dir,
                download_media=download_ids is None or msg.id in download_ids,
            )

    return list(await asyncio.gather(*(bounded(msg) for msg in msgs)))


def _download_ids(
    msgs: list,
    download_kinds: set[str],
    max_downloads: int,
    *,
    priority: str,
) -> set[int] | None:
    """Return ids whose media should be downloaded, or ``None`` for all.

    ``priority`` is independent of the input iterator order. Daily bounded
    exports prefer the newest eligible media, while incremental cursor pages
    and targeted id fetches prefer the oldest eligible media. In particular,
    capping an incremental page must not download the tail while the caller
    advances its high-water mark across the head of the backlog.

    Selection never reorders or removes manifest entries; it only controls
    which exact message ids perform a media download.
    """
    if not download_kinds and max_downloads == 0:
        return None
    if priority not in {"oldest", "newest"}:
        raise ValueError(f"unknown download priority: {priority}")
    unknown = download_kinds - MEDIA_KINDS
    if unknown:
        raise ValueError(f"unknown download kinds: {', '.join(sorted(unknown))}")
    kinds = download_kinds or MEDIA_KINDS
    eligible = [msg for msg in msgs if media_kind(msg) in kinds]
    if max_downloads > 0 and len(eligible) > max_downloads:
        eligible = sorted(
            eligible, key=lambda msg: msg.id, reverse=priority == "newest"
        )[:max_downloads]
    return {msg.id for msg in eligible}


async def iter_selected_messages(client, entity, *, start, end, limit: int,
                                 min_id: int):
    """Yield the bounded export stream in delivery order.

    In incremental mode, ``min_id`` is the durable cursor. The caller's rolling
    date window is not a second lower bound: messages synced after a multi-day
    Telegram outage can be older than ``start`` while still being undelivered.
    """
    if min_id > 0:
        # `reverse=True` walks oldest -> newest above the exclusive offset. The
        # server-side limit therefore spends the page budget on the oldest
        # backlog and cannot advance the seen high-water mark past a gap.
        it = client.iter_messages(
            entity, limit=limit, reverse=True, offset_id=min_id)
        async for msg in it:
            if msg.date >= end:
                break
            yield msg
        return

    # Non-incremental/manual date export keeps its original bounded window.
    it = client.iter_messages(entity, limit=limit, offset_date=end)
    async for msg in it:
        md = msg.date
        if md >= end:
            continue
        if md < start:
            break
        yield msg


def _parse_request(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each request must be an object")
    required = ("chat_id", "since", "until", "out_dir", "limit")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"request missing: {', '.join(missing)}")
    only_ids_raw = raw.get("only_ids") or []
    kinds_raw = raw.get("download_kinds") or []
    if not isinstance(only_ids_raw, list) or not isinstance(kinds_raw, list):
        raise ValueError("only_ids and download_kinds must be lists")
    request = {
        "chat_id": str(raw["chat_id"]),
        "since": str(raw["since"]),
        "until": str(raw["until"]),
        "out_dir": str(raw["out_dir"]),
        "limit": int(raw["limit"]),
        "min_id": int(raw.get("min_id", 0)),
        "only_ids": [int(value) for value in only_ids_raw],
        "download_kinds": {str(value) for value in kinds_raw},
        "max_downloads": int(raw.get("max_downloads", 0)),
    }
    if request["limit"] < 0 or request["min_id"] < 0 \
            or request["max_downloads"] < 0:
        raise ValueError("limit, min_id and max_downloads must be non-negative")
    date.fromisoformat(request["since"])
    date.fromisoformat(request["until"])
    unknown = request["download_kinds"] - MEDIA_KINDS
    if unknown:
        raise ValueError(f"unknown download kinds: {', '.join(sorted(unknown))}")
    return request


async def dump_request(client, request: dict) -> list[dict]:
    """Execute one validated request using an already-connected client."""
    chat_id = int(request["chat_id"])
    since = request["since"]
    until = request["until"]
    out_dir = request["out_dir"]
    limit = request["limit"]
    min_id = request["min_id"]
    only_ids = request["only_ids"]
    download_kinds = request["download_kinds"]
    max_downloads = request["max_downloads"]
    os.makedirs(out_dir, exist_ok=True)

    out: list[dict] = []
    entity = await client.get_entity(chat_id)
    if only_ids:
        msgs = await client.get_messages(entity, ids=only_ids)
        ordered = sorted((m for m in msgs if m is not None), key=lambda m: m.id)
        download_ids = _download_ids(
            ordered, download_kinds, max_downloads, priority="oldest")
        return await entries_for(ordered, out_dir, download_ids=download_ids)

    start = datetime.combine(
        date.fromisoformat(since), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    end = datetime.combine(
        date.fromisoformat(until), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    incremental = min_id > 0
    selected = [msg async for msg in iter_selected_messages(
        client, entity, start=start, end=end, limit=limit, min_id=min_id)]
    download_ids = _download_ids(
        selected, download_kinds, max_downloads,
        priority="oldest" if incremental else "newest",
    )
    out.extend(await entries_for(selected, out_dir, download_ids=download_ids))
    if not incremental:
        out.reverse()  # descending iteration → manifest must be oldest → newest
    return out


def _is_connection_failure(exc: BaseException, client) -> bool:
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    try:
        return not client.is_connected()
    except Exception:
        return False


async def batch_main() -> int:
    try:
        payload = json.load(sys.stdin)
        raw_requests = (payload.get("requests") if isinstance(payload, dict)
                        else payload if isinstance(payload, list) else None)
        if not isinstance(raw_requests, list) or not raw_requests:
            raise ValueError("payload requires a non-empty requests list")
        requests = [_parse_request(raw) for raw in raw_requests]
    except Exception as exc:
        print(f"invalid batch request: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    results: list[dict] = []
    try:
        async with tc.connect() as client:
            for request in requests:
                chat_id = request["chat_id"]
                try:
                    manifest = await dump_request(client, request)
                except Exception as exc:
                    if _is_connection_failure(exc, client):
                        raise
                    rate_limited = type(exc).__name__ == "FloodWaitError"
                    result = {
                        "chat_id": chat_id,
                        "status": "rate_limited" if rate_limited else "failed",
                        "manifest": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    if rate_limited and getattr(exc, "seconds", None) is not None:
                        result["retry_after_seconds"] = int(exc.seconds)
                    results.append(result)
                    print(f"media batch {result['status']} for {chat_id}: {result['error']}",
                          file=sys.stderr)
                    continue
                results.append({
                    "chat_id": chat_id,
                    "status": "ok",
                    "manifest": manifest,
                    "error": None,
                })
    except Exception as exc:
        print(f"media batch connection failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    print(json.dumps({
        "ok": True,
        "schema_version": "1",
        "data": {"results": results},
    }, ensure_ascii=False))
    return 0


async def legacy_main() -> int:
    request = _parse_request({
        "chat_id": sys.argv[1],
        "since": sys.argv[2],
        "until": sys.argv[3],
        "out_dir": sys.argv[4],
        "limit": sys.argv[5],
        "min_id": sys.argv[6] if len(sys.argv) > 6 else 0,
        "only_ids": ([int(x) for x in sys.argv[7].split(",") if x.strip()]
                     if len(sys.argv) > 7 else []),
        "download_kinds": ([value for value in sys.argv[8].split(",") if value]
                           if len(sys.argv) > 8 else []),
        "max_downloads": sys.argv[9] if len(sys.argv) > 9 and sys.argv[9] else 0,
    })
    async with tc.connect() as client:
        out = await dump_request(client, request)
    print(json.dumps(out, ensure_ascii=False))
    return 0


async def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--batch-json":
        return await batch_main()
    return await legacy_main()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
