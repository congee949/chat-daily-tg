"""Fetch new videos from whitelisted Bilibili UPs via the local opencli CLI.

Data path (deviation from the design doc's original feed-based plan): the
`opencli bilibili feed` output carries only display names — no uid, no bvid
field, no cover, no precise timestamp — so uid whitelisting cannot be applied
to it. Instead each whitelisted UP is polled with `user-videos <uid>` (uid is
known from the query itself), then every candidate bvid not yet in the
SeenStore gets one `video <bvid>` detail call for cover / duration /
description / precise publish time. 23 UPs × 4 runs/day is well within the
low-frequency automation guardrail.

opencli depends on a local daemon + Chrome browser bridge, which may be absent
under launchd (cold boot, Chrome quit) — probe_bridge() distinguishes that
failure mode from a login expiry so the alert message tells the user the right
fix.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import re
import subprocess
import time

from chat_daily_tg.config import BilibiliSource
from chat_daily_tg.raw_seen import SeenStore

log = logging.getLogger(__name__)

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AUTHOR_MID_RE = re.compile(r"^(.*?)\s*\(mid:\s*(\d+)\)\s*$")


class OpencliError(RuntimeError):
    """opencli subprocess failed after retries (timeout, non-zero exit, bad JSON)."""


class BridgeUnavailableError(OpencliError):
    """opencli daemon / Chrome bridge is down — not a login problem."""


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


def fetch_new_videos(src: BilibiliSource, seen: SeenStore,
                     *, now: datetime | None = None,
                     retry_max_attempts: int = 3,
                     retry_backoff_seconds: list[int] | None = None) -> list[BiliVideo]:
    """Poll每个白名单 UP 的最新视频，去重后补详情，按发布时间倒序返回。

    A single UP failing (deleted account, transient error) only logs — the
    other UPs' videos still ship. Candidates whose detail call fails are
    dropped for this run and retried next run (they are only marked seen after
    a successful send, by the caller).
    """
    now = now or datetime.now()
    cutoff = now - timedelta(hours=src.fetch.lookback_hours)
    # user-videos `date` is day-granular; compare on dates to avoid dropping a
    # same-day video published before the cutoff's time-of-day.
    cutoff_day = cutoff.date()
    blacklist_uids = {u.uid for u in src.fetch.blacklist}
    opts = dict(timeout=src.opencli.timeout_seconds, profile=src.opencli.profile,
                retry_max_attempts=retry_max_attempts,
                retry_backoff_seconds=retry_backoff_seconds)

    candidates: list[tuple[int, str]] = []  # (uid, bvid)
    for up in src.fetch.whitelist:
        if up.uid in blacklist_uids:
            continue
        try:
            rows = run_opencli(["user-videos", str(up.uid),
                                "--limit", str(src.fetch.per_up_limit)], **opts)
        except OpencliError as e:
            log.warning("user-videos failed for uid=%s (%s): %s", up.uid, up.name or "?", e)
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

    videos.sort(key=lambda v: v.publish_time or datetime.min, reverse=True)
    if len(videos) > src.fetch.max_per_digest:
        log.info("digest capped: %d -> %d videos", len(videos), src.fetch.max_per_digest)
        videos = videos[:src.fetch.max_per_digest]
    return videos
