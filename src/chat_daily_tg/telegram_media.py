"""Fetch a Telegram chat's images for a date window via telethon (tg_media_dump),
wrapping them as MediaCandidates so the existing vision pipeline can analyze them.

Why this exists: the tg-cli text exporter (telegram_exporter) reads messages.db,
which carries NO media — kabi-tg-cli stores text only. This module is the image-only
side path: it reuses the telethon downloader (private_media.dump_channel) that already
backs the channel forwarder, and turns downloaded photos into MediaCandidates.

It is gated on vision being enabled (downloading images is pointless if nothing
analyzes them) and is failure-isolated: any telethon/session/download error logs a
warning and returns [], so the text daily report is never blocked.
"""
from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import Mapping, Sequence

from chat_daily_tg.archive import safe_filename
from chat_daily_tg.media import MediaCandidate, score_media_context
from chat_daily_tg.private_media import DumpManyUnsupported, dump_channel, dump_channels

log = logging.getLogger(__name__)

# Already-downloaded TG photos clear the vision prefilter regardless of caption text.
# media.py's score is tuned for WeChat chat context (活动/价格/额度…); channel and group
# photo captions rarely hit those keywords, so the keyword score alone would drop most
# images below the 0.45 prefilter and vision would never see them. We paid the download
# cost, so let vision look — its value_score postfilter (>=0.65) does the real selection.
_MIN_DOWNLOADED_SCORE = 0.5


def media_candidates_from_manifest(
    manifest: Sequence[Mapping], *, chat_name: str, max_photos: int = 20,
) -> list[MediaCandidate]:
    """Convert one oldest→newest downloader manifest into vision candidates.

    This is intentionally pure: batch and legacy download paths use the same
    filtering, score floor, newest-photo cap, and ordering rules.
    """
    candidates: list[MediaCandidate] = []
    for entry in manifest:
        if not isinstance(entry, Mapping):
            continue
        media = entry.get("media", [])
        if not isinstance(media, Sequence) or isinstance(media, (str, bytes)):
            continue
        for md in media:
            if not isinstance(md, Mapping):
                continue
            if md.get("kind") != "photo" or not md.get("path"):
                continue
            text = entry.get("text", "") or ""
            score, reason = score_media_context(text, has_local_path=True)
            candidates.append(MediaCandidate(
                platform="Telegram",
                group_name=chat_name,
                timestamp=entry.get("date", ""),
                sender_name="",
                media_type="图片",
                local_path=str(md["path"]),
                context=text,
                reason=reason,
                score=max(score, _MIN_DOWNLOADED_SCORE),
                raw_ref=f"msg_id={entry.get('msg_id')}",
            ))
    if max_photos >= 0 and len(candidates) > max_photos:
        log.info("telegram media for %s: %d photos, capping to most recent %d",
                 chat_name, len(candidates), max_photos)
        candidates = candidates[-max_photos:] if max_photos else []
    return candidates


def _media_dir(out_dir: Path, chat_name: str) -> Path:
    return out_dir / "tg_media" / safe_filename(chat_name)


def export_chat_media(
    *,
    chat_id: str,
    chat_name: str,
    since: str,
    until: str,
    out_dir: Path,
    limit: int = 500,
    max_photos: int = 20,
) -> list[MediaCandidate]:
    """Download a TG chat's photos for [since, until) and wrap them as MediaCandidates.

    Only photos are kept — tg_media_dump also downloads video/audio/document, but the
    vision prompt targets still images, so other kinds are skipped here. Returns [] on
    any failure (logged) so the caller's text export is unaffected.

    max_photos caps how many photos this chat contributes to the vision step: vision
    runs ~50s/image serially, so an unusually image-heavy day on a high-`limit` chat
    could otherwise stall the unattended daily run. On overflow the MOST RECENT photos
    are kept (manifest is oldest→newest), since the daily report favors fresh content.
    """
    # A zero photo budget is an explicit caller decision (for example when the
    # vision stage is configured to inspect no images).  Do not start the
    # Telethon subprocess/session just to download files that the next stage
    # will deterministically discard.
    if max_photos == 0:
        log.info("telegram media disabled for %s (max_photos=0)", chat_name)
        return []
    media_dir = _media_dir(out_dir, chat_name)
    try:
        # The daily vision path only consumes photos and caps at the newest
        # max_photos. Push that policy into the downloader so non-photo media
        # and older photos are never downloaded over Telethon.
        manifest = dump_channel(
            chat_id, since, until, media_dir, limit,
            media_kinds=("photo",), max_media=max_photos,
        )
    except Exception as e:
        log.warning("telegram media fetch failed for %s: %s", chat_name, e)
        return []

    candidates = media_candidates_from_manifest(
        manifest, chat_name=chat_name, max_photos=max_photos)
    log.info("telegram media fetched for %s: %d photos", chat_name, len(candidates))
    return candidates


def export_chat_media_batch(
    requests: Sequence[Mapping], *, max_photos: int = 20,
) -> list[MediaCandidate]:
    """Download photos for several chats through one subprocess/session.

    The caller supplies one mapping per chat with ``chat_id``, ``chat_name``,
    ``since``, ``until``, ``out_dir`` and optional ``limit``. Per-chat failures
    are logged and isolated; an unsupported protocol is raised distinctly so a
    compatibility caller may choose the legacy one-chat path.
    """
    if not requests or max_photos == 0:
        if requests and max_photos == 0:
            log.info("telegram media batch disabled (max_photos=0)")
        return []
    wire: list[dict] = []
    names: list[str] = []
    for request in requests:
        names.append(str(request["chat_name"]))
        wire.append({
            "chat_id": str(request["chat_id"]),
            "since": str(request["since"]),
            "until": str(request["until"]),
            "out_dir": _media_dir(Path(request["out_dir"]), names[-1]),
            "limit": int(request.get("limit", 500)),
            "media_kinds": ["photo"],
            "max_media": max_photos,
        })
    try:
        results = dump_channels(wire)
    except DumpManyUnsupported:
        raise
    except Exception as exc:
        log.warning("telegram media batch failed: %s", exc)
        return []

    all_candidates: list[MediaCandidate] = []
    for request, result in zip(requests, results):
        name = str(request["chat_name"])
        status = result.get("status")
        if status != "ok":
            log.warning(
                "telegram media batch %s for %s: %s",
                status or "failed", name, result.get("error") or "no detail",
            )
            continue
        manifest = result.get("manifest")
        if not isinstance(manifest, list):
            log.warning("telegram media batch returned invalid manifest for %s", name)
            continue
        candidates = media_candidates_from_manifest(
            manifest, chat_name=name, max_photos=max_photos)
        all_candidates.extend(candidates)
        log.info("telegram media fetched for %s: %d photos", name, len(candidates))
    return all_candidates
