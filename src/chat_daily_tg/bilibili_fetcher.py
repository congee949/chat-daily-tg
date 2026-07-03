"""Fetch new videos from whitelisted Bilibili UPs.

Two transports, selected by `sources.bilibili.transport`:

- "api" (default): direct Bilibili Web API over httpx. The medialist endpoint
  (`x/v2/medialist/resource/list`, biz_id=<mid>) returns a UP's latest videos
  with cover / precise unix pubtime / duration / stats and — verified
  2026-07-02 — needs NO cookies and NO WBI signing (unlike `x/space/wbi/
  arc/search`, which 风控-352s without a logged-in session). Description is
  filled per NEW video from the equally cookie-free view API. No browser, no
  login state, runs anywhere (Mac / r4s).
- "opencli": the original local-Chrome-bridge path, kept as fallback should
  the undocumented medialist endpoint ever tighten. Each whitelisted UP is
  polled with `user-videos <uid>`, new bvids enriched via `video <bvid>`.
  Depends on a local daemon + Chrome, which may be absent under launchd —
  probe_bridge() distinguishes that from a login expiry.

Both transports poll by uid (display names are mutable) and share dedup,
lookback filtering, sorting, and the digest cap.

IMPORTANT: API-transport requests are made with trust_env=False. The launchd
guard exports HTTPS_PROXY for the Telegram push; letting Bilibili calls ride
that proxy means an overseas exit IP — exactly what trips 风控.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import re
import subprocess
import time

import httpx

from chat_daily_tg.config import BilibiliSource
from chat_daily_tg.raw_seen import SeenStore

log = logging.getLogger(__name__)

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AUTHOR_MID_RE = re.compile(r"^(.*?)\s*\(mid:\s*(\d+)\)\s*$")


class FetchError(RuntimeError):
    """A transport-level fetch failure (also raised when EVERY whitelisted UP
    fails in one run — a dead transport must alert, not silently push zero)."""


class OpencliError(FetchError):
    """opencli subprocess failed after retries (timeout, non-zero exit, bad JSON)."""


class BridgeUnavailableError(OpencliError):
    """opencli daemon / Chrome bridge is down — not a login problem."""


class BiliApiError(FetchError):
    """Bilibili Web API returned non-zero code (e.g. -352 风控) or bad payload."""


@dataclass(frozen=True)
class BiliVideo:
    bvid: str
    title: str
    author: str
    uid: int
    url: str
    cover: str | None = None
    duration: str | None = None          # human form, e.g. "8m4s"
    publish_time: datetime | None = None  # naive local time
    description: str = ""
    view: int | None = None

    @property
    def seen_key(self) -> str:
        return f"bilibili:{self.bvid}"


def seen_key_for(bvid: str) -> str:
    return f"bilibili:{bvid}"


def run_opencli(args: list[str], *, timeout: float = 60.0, profile: str | None = None,
                retry_max_attempts: int = 3, retry_backoff_seconds: list[int] | None = None):
    """Run one `opencli bilibili …` command and return its parsed JSON output.

    Always passes `--window background` so a launchd run without a foreground
    Chrome window still works. Retries with backoff on failure/timeout.
    """
    backoff = retry_backoff_seconds or [5, 15]
    cmd = ["opencli"]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["bilibili", *args, "-f", "json", "--window", "background"]
    last_err = ""
    for attempt in range(1, retry_max_attempts + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_err = f"timeout after {timeout}s"
        else:
            if r.returncode == 0:
                try:
                    return json.loads(r.stdout)
                except json.JSONDecodeError:
                    last_err = f"bad JSON output: {r.stdout[:200]!r}"
            else:
                last_err = (r.stderr or r.stdout or "").strip()[-300:]
        log.warning("opencli %s failed (attempt %d/%d): %s",
                    " ".join(args[:2]), attempt, retry_max_attempts, last_err)
        if attempt < retry_max_attempts:
            time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    raise OpencliError(f"opencli bilibili {' '.join(args)} failed: {last_err}")


def probe_bridge() -> None:
    """Fail fast when the opencli daemon / Chrome bridge is down (the common
    launchd cold-environment failure). Healthy `opencli doctor` exits 0 and
    prints '[OK] Connectivity'. Raises BridgeUnavailableError otherwise."""
    try:
        r = subprocess.run(["opencli", "doctor"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise BridgeUnavailableError(f"opencli doctor unreachable: {e}") from e
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "Connectivity: connected" not in out:
        raise BridgeUnavailableError(
            f"opencli bridge unhealthy (rc={r.returncode}): {out.strip()[-300:]}"
        )


def _parse_detail(rows) -> dict[str, str]:
    """`opencli bilibili video <bvid>` returns [{'field':…, 'value':…}, …]."""
    out: dict[str, str] = {}
    for row in rows or []:
        if isinstance(row, dict) and "field" in row:
            out[str(row["field"])] = str(row.get("value", ""))
    return out


def _parse_publish_time(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _duration_human(s: str) -> str | None:
    # detail gives "8m4s (484s)" — keep the human half
    return s.split("(")[0].strip() or None if s else None


# ---------------------------------------------------------------------------
# API transport (default): direct Bilibili Web API, no cookies / signing.

_API_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
}
_MEDIALIST_URL = "https://api.bilibili.com/x/v2/medialist/resource/list"
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
# Pause between per-UP list calls: 23 UPs hourly is already low-frequency, the
# spacing just avoids a burst profile (触发限流降频，不绕过).
_API_CALL_SPACING_SECONDS = 1.0


def _fmt_duration(seconds: int) -> str | None:
    if seconds <= 0:
        return None
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s"


def _api_get(client: httpx.Client, url: str, params: dict) -> dict:
    r = client.get(url, params=params)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise BiliApiError(f"bilibili api code={d.get('code')} msg={str(d.get('message'))[:80]}")
    return d.get("data") or {}


def _fetch_via_api(src: BilibiliSource, seen: SeenStore, *, now: datetime) -> list[BiliVideo]:
    """One medialist call per whitelisted UP (precise pubtime → exact lookback
    filtering, no day-granularity pass), then one view call per NEW video for
    the description. A single UP failing only logs; ALL UPs failing raises."""
    cutoff = now - timedelta(hours=src.fetch.lookback_hours)
    blacklist_uids = {u.uid for u in src.fetch.blacklist}
    ups = [u for u in src.fetch.whitelist if u.uid not in blacklist_uids]
    videos: list[BiliVideo] = []
    failures = 0
    # trust_env=False: MUST NOT ride the guard's HTTPS_PROXY (overseas exit → 风控).
    # HTTPTransport(retries=2) retries CONNECT-level blips so one transient network
    # hiccup doesn't escalate into the all-fail alarm.
    with httpx.Client(timeout=src.opencli.timeout_seconds, headers=_API_HEADERS,
                      trust_env=False,
                      transport=httpx.HTTPTransport(retries=2)) as client:
        for i, up in enumerate(ups):
            if i:
                time.sleep(_API_CALL_SPACING_SECONDS)
            try:
                data = _api_get(client, _MEDIALIST_URL, params={
                    "mobi_app": "web", "type": 1, "biz_id": up.uid, "otype": 2,
                    "ps": src.fetch.per_up_limit, "direction": "false",
                    "desc": "true", "sort_field": 1, "tid": 0, "with_current": "false",
                })
            except BiliApiError as e:
                if "code=-352" in str(e):
                    # 风控 is an IP-level verdict, not per-UP state — hammering the
                    # remaining UPs would keep pressuring a flagged IP (降频不绕过).
                    raise BiliApiError(f"-352 风控 on uid={up.uid}, aborting run: {e}") from e
                log.warning("medialist failed for uid=%s (%s): %s", up.uid, up.name or "?", e)
                failures += 1
                continue
            except Exception as e:
                log.warning("medialist failed for uid=%s (%s): %s", up.uid, up.name or "?", e)
                failures += 1
                continue
            for m in data.get("media_list") or []:
                # Per-item isolation: one dirty entry (string pubtime, ms-scale
                # timestamp, bad duration) must not kill the whole run.
                try:
                    video = _parse_media_item(m, up, seen, cutoff, client)
                except Exception as e:
                    log.warning("bad medialist item for uid=%s skipped: %s", up.uid, e)
                    continue
                if video is not None:
                    videos.append(video)
    if ups and failures == len(ups):
        raise BiliApiError(f"all {len(ups)} UP medialist fetches failed — transport dead?")
    return videos


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_media_item(m: dict, up, seen: SeenStore, cutoff: datetime,
                      client: httpx.Client) -> BiliVideo | None:
    """One medialist entry → BiliVideo, or None if invalid/seen/outside window."""
    bvid = str(m.get("bv_id") or "")
    if not _BVID_RE.fullmatch(bvid):
        return None
    pubtime = _as_int(m.get("pubtime"))
    pub = datetime.fromtimestamp(pubtime) if pubtime else None
    if seen_key_for(bvid) in seen or (pub is not None and pub < cutoff):
        return None
    desc = ""
    try:
        time.sleep(_API_CALL_SPACING_SECONDS / 2)
        view = _api_get(client, _VIEW_URL, params={"bvid": bvid})
        desc = str(view.get("desc") or "")
    except Exception as e:
        log.warning("view failed for %s (desc omitted): %s", bvid, e)
    return BiliVideo(
        bvid=bvid,
        title=str(m.get("title") or bvid),
        author=str((m.get("upper") or {}).get("name") or up.name or ""),
        uid=up.uid,
        url=f"https://www.bilibili.com/video/{bvid}",
        cover=str(m.get("cover") or "") or None,
        duration=_fmt_duration(_as_int(m.get("duration")) or 0),
        publish_time=pub,
        description=desc,
        # int-coerce: 隐藏播放量等场景上游会给字符串，脏值透传会在
        # card_caption 的 f"{view:,}" 处炸掉整轮推送
        view=_as_int((m.get("cnt_info") or {}).get("play")),
    )


def fetch_new_videos(src: BilibiliSource, seen: SeenStore,
                     *, now: datetime | None = None,
                     retry_max_attempts: int = 3,
                     retry_backoff_seconds: list[int] | None = None) -> list[BiliVideo]:
    """Poll每个白名单 UP 的最新视频，去重后补详情，按发布时间倒序返回。

    A single UP failing (deleted account, transient error) only logs — the
    other UPs' videos still ship; every UP failing raises FetchError so the
    caller alerts instead of silently pushing nothing forever. Candidates
    whose detail call fails are dropped for this run and retried next run
    (they are only marked seen after a successful send, by the caller).
    """
    now = now or datetime.now()
    if src.transport == "api":
        videos = _fetch_via_api(src, seen, now=now)
        return _finalize(videos, src.fetch.max_per_digest)
    videos = _fetch_via_opencli(src, seen, now=now,
                                retry_max_attempts=retry_max_attempts,
                                retry_backoff_seconds=retry_backoff_seconds)
    return _finalize(videos, src.fetch.max_per_digest)


def _finalize(videos: list[BiliVideo], max_per_digest: int) -> list[BiliVideo]:
    # In-run bvid dedup: 联合投稿 lists the same video under every co-author's
    # space, which would otherwise push duplicate cards within one digest.
    unique: dict[str, BiliVideo] = {}
    for v in videos:
        unique.setdefault(v.bvid, v)
    videos = list(unique.values())
    videos.sort(key=lambda v: v.publish_time or datetime.min, reverse=True)
    if len(videos) > max_per_digest:
        log.info("digest capped: %d -> %d videos", len(videos), max_per_digest)
        videos = videos[:max_per_digest]
    return videos


def _fetch_via_opencli(src: BilibiliSource, seen: SeenStore, *, now: datetime,
                       retry_max_attempts: int = 3,
                       retry_backoff_seconds: list[int] | None = None) -> list[BiliVideo]:
    cutoff = now - timedelta(hours=src.fetch.lookback_hours)
    # user-videos `date` is day-granular; compare on dates to avoid dropping a
    # same-day video published before the cutoff's time-of-day.
    cutoff_day = cutoff.date()
    blacklist_uids = {u.uid for u in src.fetch.blacklist}
    opts = dict(timeout=src.opencli.timeout_seconds, profile=src.opencli.profile,
                retry_max_attempts=retry_max_attempts,
                retry_backoff_seconds=retry_backoff_seconds)

    ups = [u for u in src.fetch.whitelist if u.uid not in blacklist_uids]
    failures = 0
    candidates: list[tuple[int, str]] = []  # (uid, bvid)
    for up in ups:
        try:
            rows = run_opencli(["user-videos", str(up.uid),
                                "--limit", str(src.fetch.per_up_limit)], **opts)
        except OpencliError as e:
            log.warning("user-videos failed for uid=%s (%s): %s", up.uid, up.name or "?", e)
            failures += 1
            continue
        for row in rows or []:
            m = _BVID_RE.search(str(row.get("url", "")))
            if not m:
                continue
            bvid = m.group(1)
            if seen_key_for(bvid) in seen:
                continue
            d = _parse_publish_time(str(row.get("date", "")))
            # Unparseable date (e.g. relative form) → keep; the detail call's
            # precise publish_time filters it below.
            if d is not None and d.date() < cutoff_day:
                continue
            candidates.append((up.uid, bvid))

    if ups and failures == len(ups):
        raise OpencliError(f"all {len(ups)} UP user-videos fetches failed — transport dead?")

    videos: list[BiliVideo] = []
    for uid, bvid in candidates:
        try:
            detail = _parse_detail(run_opencli(["video", bvid], **opts))
        except OpencliError as e:
            log.warning("video detail failed for %s: %s", bvid, e)
            continue
        pub = _parse_publish_time(detail.get("publish_time", ""))
        if pub is not None and pub < cutoff:
            continue
        author = detail.get("author", "")
        m = _AUTHOR_MID_RE.match(author)
        if m:
            author = m.group(1)
        try:
            view = int(detail.get("view", ""))
        except ValueError:
            view = None
        videos.append(BiliVideo(
            bvid=bvid,
            title=detail.get("title", "") or bvid,
            author=author,
            uid=uid,
            url=f"https://www.bilibili.com/video/{bvid}",
            cover=detail.get("thumbnail") or None,
            duration=_duration_human(detail.get("duration", "")),
            publish_time=pub,
            description=detail.get("description", ""),
            view=view,
        ))

    return videos
