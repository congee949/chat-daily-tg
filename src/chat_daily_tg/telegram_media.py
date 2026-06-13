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

from chat_daily_tg.archive import safe_filename
from chat_daily_tg.media import MediaCandidate, score_media_context
from chat_daily_tg.private_media import dump_channel

log = logging.getLogger(__name__)

# Already-downloaded TG photos clear the vision prefilter regardless of caption text.
# media.py's score is tuned for WeChat chat context (活动/价格/额度…); channel and group
# photo captions rarely hit those keywords, so the keyword score alone would drop most
# images below the 0.45 prefilter and vision would never see them. We paid the download
# cost, so let vision look — its value_score postfilter (>=0.65) does the real selection.
_MIN_DOWNLOADED_SCORE = 0.5


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
    media_dir = out_dir / "tg_media" / safe_filename(chat_name)
    try:
        manifest = dump_channel(chat_id, since, until, media_dir, limit)
    except Exception as e:
        log.warning("telegram media fetch failed for %s: %s", chat_name, e)
        return []

    candidates: list[MediaCandidate] = []
    for entry in manifest:
        for md in entry.get("media", []):
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
                local_path=md["path"],
                context=text,
                reason=reason,
                score=max(score, _MIN_DOWNLOADED_SCORE),
                raw_ref=f"msg_id={entry.get('msg_id')}",
            ))
    if len(candidates) > max_photos:
        log.info("telegram media for %s: %d photos, capping to most recent %d",
                 chat_name, len(candidates), max_photos)
        candidates = candidates[-max_photos:]
    log.info("telegram media fetched for %s: %d photos", chat_name, len(candidates))
    return candidates
