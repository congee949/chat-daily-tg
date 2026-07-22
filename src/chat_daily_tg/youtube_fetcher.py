"""Fetch new videos from whitelisted YouTube channels.

Transport normally uses per-channel RSS
(`youtube.com/feeds/videos.xml?channel_id=UC…`) — no login, no cookies, no
API quota, works headless anywhere. If every RSS feed is unavailable, the
same YouTube Data API key discovers recent uploads through `channels.list` +
`playlistItems.list`; this prevents a transient RSS outage from turning into
a failed digest. RSS carries no duration, so candidates are enriched in two
tiers: batched `videos.list` (YouTube Data API v3, 1 quota unit per 50 ids)
first, then a per-video watch-page scrape (`"lengthSeconds"`) for whatever
the API didn't answer — duration + view count power the Shorts filter
(长视频 digest 的定位).

Proxy contract is the OPPOSITE of bilibili_fetcher: YouTube / googleapis /
i.ytimg.com are unreachable from a China exit, so every request here MUST
ride the wrapper's HTTP(S)_PROXY (bwg tinyproxy over tailscale on r4s).
CAUTION: passing an explicit `transport=` to httpx.Client BYPASSES trust_env
proxy mounting — the 2026-07-19 deploy dry-run went silently direct that way
and every feed died in the TLS handshake (GFW EOF). The proxy is therefore
plumbed into the HTTPTransport by hand (_proxy_from_env); do NOT "simplify"
it away, and do NOT add trust_env=False anywhere on the YouTube path.
env.scrub_socks_proxy_env() only pops ALL_PROXY (socks), the http proxy vars
these requests need survive it.

Enrichment failure degrades, never blocks (投递优先于完美): videos ship
without duration/views, with a #shorts-in-title heuristic as the only filter.
Skipped Shorts are NOT marked seen — no journal-less seen write; they simply
re-enter candidates until the lookback window ages them out (cost: their ids
stay in the one batched videos.list call for ~48h).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

from chat_daily_tg.config import YoutubeSource
from chat_daily_tg.raw_seen import SeenStore

log = logging.getLogger(__name__)


class YoutubeFetchError(RuntimeError):
    """Transport-level fetch failure (also raised when EVERY whitelisted
    channel fails in one run — a dead transport must alert, not silently push
    zero forever)."""


@dataclass(frozen=True)
class YtVideo:
    video_id: str
    title: str
    author: str
    channel_id: str
    url: str
    cover: str | None = None
    duration: str | None = None            # human form, e.g. "12m40s"
    duration_seconds: int | None = None    # None until Data API enrichment
    publish_time: datetime | None = None   # naive local time
    description: str = ""
    view: int | None = None
    topic: str | None = None               # per-channel forum-topic override

    @property
    def seen_key(self) -> str:
        return seen_key_for(self.video_id)


def seen_key_for(video_id: str) -> str:
    return f"youtube:{video_id}"


_FEED_URL = "https://www.youtube.com/feeds/videos.xml"
_CHANNELS_API_URL = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST_ITEMS_API_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}
# Pause between per-channel feed calls: ~12 channels hourly is already
# low-frequency; the spacing just avoids a burst profile.
_CALL_SPACING_SECONDS = 0.5

# Per-channel feed retry: since 2025-12 YouTube's /feeds/videos.xml flakes
# platform-side — the SAME channel 404s (or 500s) then 200s on a retry
# seconds later, with random channels affected each window (RSS-Bridge#2113,
# FreeTube#8443, n8n community threads; first hit here 2026-07-21, an all-11
# channels 404 storm that the all-fail guard correctly escalated). A 404 is
# therefore NOT proof of a deleted channel until retries are exhausted; the
# attempts below ride out the flake windows instead of alarming on them.
_FEED_ATTEMPTS = 3
_FEED_RETRY_BACKOFF_SECONDS = 3.0
# Whole-run second wave: 2026-07-21 14:45–14:57 showed multi-minute storms
# where every channel exhausted per-call retries (~9s) and the next */5 tick
# just re-alerted. One delayed full re-poll catches storms that clear within
# ~1 min without spamming the alert topic on every 5-minute gate reopen.
_FEED_WAVES = 2
_FEED_WAVE_BACKOFF_SECONDS = 45.0
# Statuses worth retrying: the flake presents as 404/500, rate limiting as
# 429; other 4xx (403 etc.) fail through immediately.
_FEED_RETRYABLE_STATUSES = {404, 429, 500, 502, 503, 504}
_API_UPLOADS_PAGE_SIZE = 50

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# ISO 8601 duration as the Data API emits it (PT#H#M#S; live/premiere gives
# "P0D"; day part shows up on >24h streams).
_ISO_DURATION_RE = re.compile(
    r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?"
)


def parse_iso_duration(s: str) -> int | None:
    m = _ISO_DURATION_RE.fullmatch(s or "")
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict().items() if v is not None}
    return (parts.get("d", 0) * 86400 + parts.get("h", 0) * 3600
            + parts.get("m", 0) * 60 + parts.get("s", 0))


def _fmt_duration(seconds: int | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s"


def _parse_published(s: str) -> datetime | None:
    """RSS `published` is offset-aware ISO 8601 (UTC) → naive LOCAL time, to
    match the naive `now`/cutoff arithmetic shared with the Bilibili path."""
    try:
        return datetime.fromisoformat(s.strip()).astimezone().replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_entry(entry: ET.Element, channel, seen: SeenStore,
                 cutoff: datetime) -> YtVideo | None:
    """One <entry> → YtVideo, or None if invalid/seen/outside window."""
    video_id = (entry.findtext("yt:videoId", "", _NS) or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z_-]{11}", video_id):
        return None
    pub = _parse_published(entry.findtext("atom:published", "", _NS))
    if seen_key_for(video_id) in seen or (pub is not None and pub < cutoff):
        return None
    group = entry.find("media:group", _NS)
    desc, cover, views = "", None, None
    if group is not None:
        desc = (group.findtext("media:description", "", _NS) or "").strip()
        thumb = group.find("media:thumbnail", _NS)
        if thumb is not None:
            cover = thumb.get("url") or None
        stats = group.find("media:community/media:statistics", _NS)
        if stats is not None:
            views = _as_int(stats.get("views"))
    return YtVideo(
        video_id=video_id,
        title=(entry.findtext("atom:title", "", _NS) or "").strip() or video_id,
        author=(entry.findtext("atom:author/atom:name", "", _NS) or "").strip()
               or (channel.name or ""),
        channel_id=channel.channel_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        cover=cover,
        publish_time=pub,
        description=desc,
        view=views,
        topic=channel.topic,
    )


def _active_channels(src: YoutubeSource) -> list:
    """Return whitelisted channels after the immutable-ID blacklist gate."""
    blacklist_ids = {c.channel_id for c in src.fetch.blacklist}
    return [c for c in src.fetch.whitelist if c.channel_id not in blacklist_ids]


def _api_thumbnail(snippet: dict) -> str | None:
    thumbnails = snippet.get("thumbnails")
    if not isinstance(thumbnails, dict):
        return None
    for size in ("maxres", "standard", "high", "medium", "default"):
        thumb = thumbnails.get(size)
        if isinstance(thumb, dict) and isinstance(thumb.get("url"), str):
            return thumb["url"]
    return None


def _parse_api_playlist_item(item, channel, seen: SeenStore,
                             cutoff: datetime) -> YtVideo | None:
    """One Data API playlist item → YtVideo, matching RSS eligibility rules."""
    if not isinstance(item, dict):
        return None
    content = item.get("contentDetails")
    snippet = item.get("snippet")
    content = content if isinstance(content, dict) else {}
    snippet = snippet if isinstance(snippet, dict) else {}
    video_id = str(content.get("videoId") or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z_-]{11}", video_id):
        return None
    pub = _parse_published(
        str(content.get("videoPublishedAt") or snippet.get("publishedAt") or ""))
    if seen_key_for(video_id) in seen or (pub is not None and pub < cutoff):
        return None
    title = str(snippet.get("title") or "").strip() or video_id
    author = (str(snippet.get("channelTitle") or "").strip()
              or (channel.name or ""))
    return YtVideo(
        video_id=video_id,
        title=title,
        author=author,
        channel_id=channel.channel_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        cover=_api_thumbnail(snippet),
        publish_time=pub,
        description=str(snippet.get("description") or "").strip(),
        topic=channel.topic,
    )


def _fetch_feed_with_retry(client: httpx.Client, channel) -> ET.Element:
    """One channel's RSS fetch with retries over the 2025-12+ flake statuses
    (see _FEED_RETRYABLE_STATUSES). Transport blips (httpx.HTTPTransport's own
    retries=2 only covers CONNECT-level failures) retry here too."""
    last_err: Exception | None = None
    for attempt in range(1, _FEED_ATTEMPTS + 1):
        try:
            r = client.get(_FEED_URL, params={"channel_id": channel.channel_id})
            r.raise_for_status()
            return ET.fromstring(r.content)
        except httpx.HTTPStatusError as e:
            last_err = e
            if e.response.status_code not in _FEED_RETRYABLE_STATUSES:
                raise
        except Exception as e:  # transport error, XML parse error
            last_err = e
        if attempt < _FEED_ATTEMPTS:
            time.sleep(_FEED_RETRY_BACKOFF_SECONDS * attempt)
    assert last_err is not None
    raise last_err


def _poll_channels(channels, seen: SeenStore, client: httpx.Client, *,
                   cutoff: datetime) -> tuple[list[YtVideo], int, list]:
    """One wave over ``channels``. Returns (videos, failure_count, failed_channels)."""
    videos: list[YtVideo] = []
    failed: list = []
    for i, channel in enumerate(channels):
        if i:
            time.sleep(_CALL_SPACING_SECONDS)
        try:
            root = _fetch_feed_with_retry(client, channel)
        except Exception as e:
            log.warning("feed failed for %s (%s): %s",
                        channel.channel_id, channel.name or "?", e)
            failed.append(channel)
            continue
        for entry in root.findall("atom:entry", _NS):
            # Per-item isolation: one dirty entry must not kill the whole run.
            try:
                video = _parse_entry(entry, channel, seen, cutoff)
            except Exception as e:
                log.warning("bad feed entry for %s skipped: %s", channel.channel_id, e)
                continue
            if video is not None:
                videos.append(video)
    return videos, len(failed), failed


def _fetch_feeds(src: YoutubeSource, seen: SeenStore, client: httpx.Client,
                 *, cutoff: datetime) -> list[YtVideo]:
    """RSS over whitelisted channels. A single channel failing only logs
    (deleted channel / transient error). ALL channels failing on the first
    wave triggers a delayed second wave; only total failure across all
    waves raises (dead transport or sustained YouTube RSS outage)."""
    channels = _active_channels(src)
    if not channels:
        return []

    videos, failures, failed_channels = _poll_channels(
        channels, seen, client, cutoff=cutoff)
    # Only a total outage triggers extra waves — partial failure is already
    # soft (skipped channel + lookback catch-up next round).
    for wave in range(2, _FEED_WAVES + 1):
        if failures < len(channels) or not failed_channels:
            break
        log.warning(
            "all %d feeds failed on wave %d; waiting %.0fs then retrying",
            len(channels), wave - 1, _FEED_WAVE_BACKOFF_SECONDS,
        )
        time.sleep(_FEED_WAVE_BACKOFF_SECONDS)
        videos, failures, failed_channels = _poll_channels(
            failed_channels, seen, client, cutoff=cutoff)
    if not videos and failures >= len(channels):
        raise YoutubeFetchError(
            f"all {len(channels)} channel feed fetches failed — transport dead?")
    return videos


def _fetch_uploads_via_api(src: YoutubeSource, seen: SeenStore,
                           client: httpx.Client, *, cutoff: datetime,
                           api_key: str) -> list[YtVideo]:
    """Discover recent uploads through Data API after a total RSS outage.

    A single batched `channels.list` resolves each channel's immutable uploads
    playlist, then one `playlistItems.list` call per channel supplies the
    recent videos. Any individual channel is soft-failed like RSS; the whole
    fallback is considered unavailable only when none of those playlists can
    be read. It is deliberately invoked *only* after total RSS failure, so a
    healthy poll retains RSS's zero-quota normal path.
    """
    channels = _active_channels(src)
    if not channels:
        return []
    try:
        response = client.get(_CHANNELS_API_URL, params={
            "part": "contentDetails",
            "id": ",".join(channel.channel_id for channel in channels),
            "key": api_key,
        })
        response.raise_for_status()
        items = response.json().get("items") or []
    except Exception as e:
        raise YoutubeFetchError(f"YouTube Data API uploads lookup failed: {e}") from e

    uploads_by_channel: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("id")
        content_details = item.get("contentDetails")
        if not isinstance(content_details, dict):
            continue
        related = content_details.get("relatedPlaylists") or {}
        uploads = related.get("uploads") if isinstance(related, dict) else None
        if isinstance(channel_id, str) and isinstance(uploads, str) and uploads:
            uploads_by_channel[channel_id] = uploads

    videos: list[YtVideo] = []
    successful_channels = 0
    for i, channel in enumerate(channels):
        if i:
            time.sleep(_CALL_SPACING_SECONDS)
        uploads = uploads_by_channel.get(channel.channel_id)
        if not uploads:
            log.warning("Data API fallback has no uploads playlist for %s (%s)",
                        channel.channel_id, channel.name or "?")
            continue
        try:
            response = client.get(_PLAYLIST_ITEMS_API_URL, params={
                "part": "snippet,contentDetails",
                "playlistId": uploads,
                "maxResults": _API_UPLOADS_PAGE_SIZE,
                "key": api_key,
            })
            response.raise_for_status()
            items = response.json().get("items") or []
            successful_channels += 1
        except Exception as e:
            log.warning("Data API fallback failed for %s (%s): %s",
                        channel.channel_id, channel.name or "?", e)
            continue
        for item in items:
            try:
                video = _parse_api_playlist_item(item, channel, seen, cutoff)
            except Exception as e:
                log.warning("bad Data API playlist item for %s skipped: %s",
                            channel.channel_id, e)
                continue
            if video is not None:
                videos.append(video)

    if not successful_channels:
        raise YoutubeFetchError("YouTube Data API uploads fallback failed for every channel")
    log.info("YouTube Data API uploads fallback succeeded for %d/%d channels; %d candidates",
             successful_channels, len(channels), len(videos))
    return videos


def _with_duration(v: YtVideo, secs: int | None, view: int | None) -> YtVideo:
    return YtVideo(
        video_id=v.video_id, title=v.title, author=v.author,
        channel_id=v.channel_id, url=v.url, cover=v.cover,
        duration=_fmt_duration(secs), duration_seconds=secs,
        publish_time=v.publish_time, description=v.description,
        view=view if view is not None else v.view, topic=v.topic,
    )


def _enrich_via_api(videos: list[YtVideo], client: httpx.Client,
                    api_key: str) -> dict[str, YtVideo]:
    """videos.list in chunks of 50 (1 quota unit each) → {video_id: enriched}.
    Only ids the API actually answered for appear in the result."""
    details: dict[str, dict] = {}
    ids = [v.video_id for v in videos]
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        try:
            r = client.get(_VIDEOS_API_URL, params={
                "part": "contentDetails,statistics", "id": ",".join(chunk),
                "key": api_key,
            })
            r.raise_for_status()
            for item in r.json().get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    details[str(item["id"])] = item
        except Exception as e:
            log.warning("videos.list enrichment failed for %d ids: %s", len(chunk), e)
    out: dict[str, YtVideo] = {}
    for v in videos:
        item = details.get(v.video_id)
        if item is None:
            continue
        secs = parse_iso_duration(str((item.get("contentDetails") or {}).get("duration") or ""))
        out[v.video_id] = _with_duration(
            v, secs, _as_int((item.get("statistics") or {}).get("viewCount")))
    return out


# ytInitialPlayerResponse fields on the watch page. Regex over the raw HTML on
# purpose — no JSON parse of a ~1MB blob; a miss just leaves the video
# un-enriched.
_LENGTH_SECONDS_RE = re.compile(r'"lengthSeconds"\s*:\s*"(\d+)"')
_VIEW_COUNT_RE = re.compile(r'"viewCount"\s*:\s*"(\d+)"')


def _enrich_via_watch_page(v: YtVideo, client: httpx.Client) -> YtVideo | None:
    """Fallback duration source when the Data API is unavailable — first hit
    for real on 2026-07-19: the deployed GOOGLE_API_KEY is console-restricted
    to Gemini APIs and 403s every youtube.v3 method. Costlier (one HTML page
    per video) but keyless, so the Shorts filter keeps working."""
    try:
        r = client.get(v.url)
        r.raise_for_status()
        m = _LENGTH_SECONDS_RE.search(r.text)
        if not m:
            return None
        view_m = _VIEW_COUNT_RE.search(r.text)
        return _with_duration(v, int(m.group(1)),
                              int(view_m.group(1)) if view_m else None)
    except Exception as e:
        log.warning("watch-page enrichment failed for %s: %s", v.video_id, e)
        return None


def _enrich_durations(videos: list[YtVideo], client: httpx.Client,
                      api_key: str | None) -> list[YtVideo]:
    """Two-tier duration/view enrichment: batched videos.list when a key is
    configured, per-video watch-page scrape for whatever the API didn't cover.
    Both tiers failing still ships the video un-enriched (投递优先于完美) —
    only the Shorts filter degrades to the #shorts-in-title heuristic."""
    if not videos:
        return videos
    enriched = _enrich_via_api(videos, client, api_key) if api_key else {}
    if not api_key:
        log.warning("no YouTube Data API key — falling back to watch-page "
                    "enrichment for %d videos", len(videos))
    out: list[YtVideo] = []
    for v in videos:
        if v.video_id in enriched:
            out.append(enriched[v.video_id])
            continue
        time.sleep(_CALL_SPACING_SECONDS / 2)
        out.append(_enrich_via_watch_page(v, client) or v)
    return out


def _is_short(v: YtVideo, min_duration_seconds: int) -> bool:
    """Known duration at/under the bar → Short. Unknown duration (enrichment
    degraded) → only the title heuristic; unknowns otherwise ship."""
    if v.duration_seconds is not None:
        return v.duration_seconds <= min_duration_seconds
    return "#shorts" in v.title.lower()


def _proxy_from_env() -> str | None:
    """The wrapper's exported proxy, read by hand: the client below passes an
    explicit HTTPTransport (for connect-level retries), and httpx skips its
    trust_env proxy mounting whenever a transport is given — without this the
    whole fetch silently goes DIRECT and dies in the TLS handshake on r4s."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v:
            return v
    return None


def fetch_new_videos(src: YoutubeSource, seen: SeenStore, *,
                     api_key: str | None,
                     now: datetime | None = None) -> list[YtVideo]:
    """Poll每个白名单频道的 RSS，去重后批量补时长，过滤 Shorts，按发布时间
    倒序返回（截断到 max_per_digest）。Candidates are only marked seen after a
    successful send, by the caller (write-after-send)."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=src.fetch.lookback_hours)
    # HTTPTransport(retries=2) absorbs CONNECT-level blips before they
    # escalate into the all-fail alarm; proxy is plumbed in explicitly (see
    # _proxy_from_env — an explicit transport bypasses trust_env mounting).
    with httpx.Client(timeout=src.fetch.timeout_seconds, headers=_HEADERS,
                      follow_redirects=True,
                      transport=httpx.HTTPTransport(retries=2,
                                                    proxy=_proxy_from_env())) as client:
        try:
            videos = _fetch_feeds(src, seen, client, cutoff=cutoff)
        except YoutubeFetchError as rss_error:
            if not api_key:
                raise
            log.warning("all YouTube RSS feeds failed; trying Data API uploads fallback")
            try:
                videos = _fetch_uploads_via_api(
                    src, seen, client, cutoff=cutoff, api_key=api_key)
            except YoutubeFetchError as api_error:
                raise YoutubeFetchError(
                    f"{rss_error}; YouTube Data API fallback also failed") from api_error
        videos = _enrich_durations(videos, client, api_key)
    kept = [v for v in videos if not _is_short(v, src.fetch.min_duration_seconds)]
    if len(kept) < len(videos):
        log.info("shorts filtered: %d -> %d videos", len(videos), len(kept))
    return _finalize(kept, src.fetch.max_per_digest)


def _finalize(videos: list[YtVideo], max_per_digest: int) -> list[YtVideo]:
    unique: dict[str, YtVideo] = {}
    for v in videos:
        unique.setdefault(v.video_id, v)
    videos = list(unique.values())
    videos.sort(key=lambda v: v.publish_time or datetime.min, reverse=True)
    if len(videos) > max_per_digest:
        log.info("digest capped: %d -> %d videos", len(videos), max_per_digest)
        videos = videos[:max_per_digest]
    return videos
