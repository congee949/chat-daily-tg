"""L1 content-level dedup for the raw-channel forwarding stage.

The exact-id ``SeenStore`` cannot see the same content re-entering through a
different channel (forwarded post → new msg_id). This module adds a content
identity with a rolling time window:

- ``text_fingerprint``   — verbatim-forward detection (the fming↔yihong case)
- ``canonical_urls``     — URL identity that survives markdown wrapping,
                           fullwidth punctuation and tracker params
- ``ContentSeenStore``   — sqlite store of fingerprints already delivered
- ``XMonitorIndex``      — read-only view of a pulled copy of x_monitor's
                           pushed index (cross-producer layer, feature-gated)
- ``check_duplicate``    — the single decision entry point

Hit policy (决策记录 2026-07-16): text fingerprint hit → skip; URL hit → skip
only when the post is a bare link (no substantive commentary); an x_monitor
hit follows the same bare-link rule. 宁可重复，不可误杀 — every ambiguity
resolves toward delivery, and the CALLER must wrap the whole check in
try/except so no failure here can ever block delivery (投递优先于完美).

Forbidden mitigations (do not "fix" races by deferring): holding a post for a
later cycle silently loses it — the SeenStore high-water mark advances past it
and incremental fetch never returns it again. Same-tick duplicates are
explicitly accepted; they cost seconds.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

# Same shape as raw_channels._MD_LINK_RE — [label](url) markdown links are
# consumed FIRST so the closing ')' of the markdown syntax can never leak into
# the URL, and the label counts as body text for the bare-link test.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

# Bare URLs terminate at whitespace, angle brackets, fullwidth brackets, CJK
# punctuation AND CJK ideographs. Chat text routinely glues Chinese prose
# straight onto a URL with no space (46 such captures in the 5-month corpus);
# terminating at ideographs cuts the glue. The cost — a rare URL with a raw
# (unencoded) CJK path truncates to a different fingerprint — only ever loses
# a suppression, never a delivery, which is the permitted failure direction.
# ASCII parens stay IN the pattern (wiki/Foo_(bar) survives; markdown links
# were already consumed, so ')' here is prose) and unbalanced trailing ')' is
# stripped afterwards.
_BARE_URL_RE = re.compile(
    r"https?://[^\s<>（）【】《》「」『』，。；：！？、一-鿿]+"
)

# Trailing sentence punctuation that belongs to the prose, not the URL.
_TRAIL_PUNCT = ".,;!?\"'。，；：！？"

_TWEET_HOSTS = {
    "twitter.com", "mobile.twitter.com", "fxtwitter.com", "vxtwitter.com",
    "fixupx.com", "fixvx.com", "x.com",
}
# /i/status/<id> (author-less — the dominant bare-link form in the corpus) and
# /<user>/status/<id>, optional /i/web prefix, optional /photo/1-style suffix.
_TWEET_PATH_RE = re.compile(
    r"^/(?:i/web/|)?(?:[A-Za-z0-9_]+/)?status(?:es)?/(\d+)(?:/|$)"
)
# Mirrors x_monitor's ARTICLE_URL_RE: x.com/i/article/<id> or /<user>/articles/<id>.
_ARTICLE_PATH_RE = re.compile(
    r"^/(?:i/article|[A-Za-z0-9_]+/articles)/([A-Za-z0-9_-]+)(?:/|$)"
)
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

# Characters that count as substance for the bare-link test: anything that is
# not whitespace / punctuation / symbols. Counted in code points, not bytes —
# 10 CJK code points is already a deliberate editorial remark.
_BARE_LINK_MAX_CHARS = 10
# Normalized text shorter than this never fingerprints («哭了»-style shortposts
# must not collide across channels).
_MIN_FINGERPRINT_CHARS = 24


def _substance_len(text: str) -> int:
    return sum(
        1 for ch in text
        if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )


def _strip_trailing_punct(url: str) -> str:
    url = url.rstrip(_TRAIL_PUNCT)
    # Unbalanced trailing ')' belongs to surrounding prose; balanced parens are
    # part of the URL (wiki/Foo_(bar) survives).
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(_TRAIL_PUNCT)
    return url


def extract_urls(text: str) -> list[str]:
    """All http(s) URLs in `text`, markdown links first, in document order."""
    if not text:
        return []
    urls: list[str] = []
    remainder_parts: list[str] = []
    pos = 0
    for m in _MD_LINK_RE.finditer(text):
        remainder_parts.append(text[pos:m.start()])
        remainder_parts.append(m.group(1))  # label rejoins the remainder (a label that is itself a URL still gets scanned)
        urls.append(m.group(2))
        pos = m.end()
    remainder_parts.append(text[pos:])
    remainder = " ".join(remainder_parts)
    for m in _BARE_URL_RE.finditer(remainder):
        u = _strip_trailing_punct(m.group(0))
        if u:
            urls.append(u)
    return urls


def canonicalize_url(url: str) -> str | None:
    """One stable identity per link target.

    Tweet links (any mirror host, any /i/web//photo decoration) → x.com/status/<id>;
    X articles → x.com/i/article/<id>; bilibili video links → BV id; everything
    else keeps its query minus utm_*/share trackers (?page=2 may be different
    content — generic hosts are canonicalized conservatively).
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    if host in _TWEET_HOSTS:
        m = _TWEET_PATH_RE.match(parts.path)
        if m:
            return f"x.com/status/{m.group(1)}"
        m = _ARTICLE_PATH_RE.match(parts.path)
        if m:
            return f"x.com/i/article/{m.group(1)}"
        # Non-status twitter link (profile, search…) — fall through to generic.

    if host.endswith("bilibili.com"):
        m = _BV_RE.search(parts.path)
        if m:
            return f"bilibili.com/video/{m.group(1)}"

    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        lk = k.lower()
        if lk.startswith("utm_") or lk.startswith("share"):
            continue
        kept.append((k, v))
    kept.sort()
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(
        (parts.scheme.lower() or "https", netloc, parts.path.rstrip("/"),
         urlencode(kept), "")
    )


def canonical_urls(text: str) -> set[str]:
    out: set[str] = set()
    for u in extract_urls(text):
        c = canonicalize_url(u)
        if c:
            out.add(c)
    return out


def tweet_keys_from_urls(urls: set[str]) -> set[str]:
    """Map canonical URLs to x_monitor pushed-index keys (t:<id> / a:<id>).

    Shared by the runtime check AND the measurement script so the overlap
    estimate and shipped behavior can never diverge.
    """
    keys: set[str] = set()
    for u in urls:
        if u.startswith("x.com/status/"):
            keys.add("t:" + u.rsplit("/", 1)[1])
        elif u.startswith("x.com/i/article/"):
            keys.add("a:" + u.rsplit("/", 1)[1])
    return keys


def _body_without_links(text: str) -> str:
    """Visible prose with markdown links reduced to their labels and bare URLs
    removed. Labels count as body: under the skip policy that biases toward
    'substantive' — the safe direction."""
    if not text:
        return ""
    body = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    return _BARE_URL_RE.sub(" ", body)


def is_bare_link_post(text: str) -> bool:
    """True when the post is essentially just a link — the only shape whose URL
    identity alone justifies a skip. ≤10 substantive code points: a hashtag or
    an emoji shrug is still bare; a 21-char Chinese editorial remark is not."""
    if not text or not text.strip():
        return False  # media-only / empty posts are never treated as bare links
    if not extract_urls(text):
        return False
    return _substance_len(_body_without_links(text)) <= _BARE_LINK_MAX_CHARS


def text_fingerprint(text: str) -> str | None:
    """sha1 over the URL-free, punctuation-free, casefolded body. None when the
    normalized body is shorter than 24 code points (short posts collide by
    coincidence, not by forwarding)."""
    if not text:
        return None
    body = _body_without_links(text)
    normalized = "".join(
        ch for ch in unicodedata.normalize("NFKC", body).casefold()
        if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )
    if len(normalized) < _MIN_FINGERPRINT_CHARS:
        return None
    return sha1(normalized.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# stores

_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_seen (
    fingerprint TEXT PRIMARY KEY,   -- 'text:<sha1>' | 'url:<sha1>'
    chat_id     TEXT NOT NULL,
    msg_id      INTEGER NOT NULL,
    channel     TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL       -- ISO UTC
);
CREATE INDEX IF NOT EXISTS idx_content_seen_sent_at ON content_seen(sent_at);
"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def text_key(fp: str) -> str:
    return f"text:{fp}"


def url_key(canonical: str) -> str:
    return f"url:{sha1(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class SeenHit:
    fingerprint: str
    chat_id: str
    msg_id: int
    channel: str
    sent_at: str


class ContentSeenStore:
    """Rolling-window fingerprint store. Registration is write-after-send only:
    a fingerprint in here means that content was DELIVERED, so a crash between
    send and register re-delivers rather than drops (same semantics as SeenStore)."""

    def __init__(self, path: Path, window_days: int = 14):
        self.window_days = window_days
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._prune()

    def _prune(self) -> None:
        cutoff = (_now_utc() - timedelta(days=self.window_days)).isoformat()
        with self._conn:
            self._conn.execute("DELETE FROM content_seen WHERE sent_at < ?", (cutoff,))

    def lookup(self, fingerprints: list[str]) -> SeenHit | None:
        if not fingerprints:
            return None
        marks = ",".join("?" for _ in fingerprints)
        row = self._conn.execute(
            f"SELECT * FROM content_seen WHERE fingerprint IN ({marks}) "
            "ORDER BY sent_at LIMIT 1",
            fingerprints,
        ).fetchone()
        if row is None:
            return None
        return SeenHit(row["fingerprint"], row["chat_id"], row["msg_id"],
                       row["channel"], row["sent_at"])

    def register(self, fingerprints: list[str], chat_id: str, msg_id: int,
                 channel: str = "") -> None:
        """INSERT OR IGNORE — the first delivery of a piece of content owns its
        fingerprint; later re-registrations never rewrite history."""
        if not fingerprints:
            return
        now = _now_utc().isoformat()
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO content_seen VALUES (?,?,?,?,?)",
                [(fp, str(chat_id), int(msg_id), channel, now) for fp in fingerprints],
            )

    def close(self) -> None:
        self._conn.close()


def fingerprints_for(text: str) -> list[str]:
    """Every fingerprint this post would register: text identity + one per URL."""
    fps: list[str] = []
    tf = text_fingerprint(text)
    if tf:
        fps.append(text_key(tf))
    fps.extend(url_key(u) for u in sorted(canonical_urls(text)))
    return fps


class XMonitorIndex:
    """Read-only view over a pulled copy of x_monitor's pushed_index.json.

    Never writes anything anywhere. Missing / malformed / stale files all load
    as empty — which only ever means fewer suppressions:
      * per-entry TTL is re-applied here (a frozen copy cannot suppress from
        entries the live index already pruned);
      * a copy older than `max_age_hours` counts as ABSENT — the four root
        causes (prod flag off / path moved / ssh broken / pull frozen) all
        present identically, and all must fail toward delivery.
    """

    def __init__(self, path: Path, ttl_days: int = 14, max_age_hours: int = 24):
        self.entries: dict[str, dict] = {}
        self.stale = False
        try:
            p = Path(path)
            if not p.exists():
                return
            age_h = (_now_utc().timestamp() - p.stat().st_mtime) / 3600
            if age_h > max_age_hours:
                self.stale = True
                log.warning(
                    "xmonitor index copy is %.0fh old (> %dh) — treating as absent",
                    age_h, max_age_hours,
                )
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("entries")
            if not isinstance(raw, dict):
                return
            cutoff = _now_utc() - timedelta(days=ttl_days)
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    ts = datetime.fromisoformat(str(entry.get("ts")))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if ts >= cutoff:
                    self.entries[str(key)] = entry
        except Exception as e:  # any load problem = empty index = deliver
            log.warning("xmonitor index copy unreadable (%s) — treating as empty", e)
            self.entries = {}

    def lookup(self, keys: set[str]) -> tuple[str, dict] | None:
        for k in sorted(keys):
            entry = self.entries.get(k)
            if entry is not None:
                # Ambiguous deliveries are indexed by x_monitor with ok=True;
                # once bwg stamps them `assumed:true` they stop counting as
                # delivered evidence here (the channel echo is the de-facto backup).
                if entry.get("assumed"):
                    continue
                return k, entry
        return None


# --------------------------------------------------------------------------- #
# decision

@dataclass(frozen=True)
class DedupDecision:
    skip: bool
    reason: str = ""          # 'text' | 'url' | 'xmon' | ''
    detail: dict = field(default_factory=dict)


def check_duplicate(
    text: str,
    *,
    store: ContentSeenStore,
    xmon: XMonitorIndex | None = None,
) -> DedupDecision:
    """The three gates, cheapest and most certain first. `text` is the
    promo-stripped plain head content of the card about to be sent.

    The caller MUST wrap this call in try/except and treat any exception as
    'no hit' — this function is allowed to assume its inputs exist.
    """
    tf = text_fingerprint(text)
    if tf:
        hit = store.lookup([text_key(tf)])
        if hit:
            return DedupDecision(True, "text", {
                "matched_chat_id": hit.chat_id, "matched_msg_id": hit.msg_id,
                "matched_channel": hit.channel, "matched_sent_at": hit.sent_at,
            })

    urls = canonical_urls(text)
    if not urls:
        return DedupDecision(False)
    bare = is_bare_link_post(text)
    if not bare:
        # Substantive commentary always delivers, whatever its links point at.
        return DedupDecision(False)

    hit = store.lookup([url_key(u) for u in sorted(urls)])
    if hit:
        return DedupDecision(True, "url", {
            "matched_chat_id": hit.chat_id, "matched_msg_id": hit.msg_id,
            "matched_channel": hit.channel, "matched_sent_at": hit.sent_at,
        })

    if xmon is not None:
        xhit = xmon.lookup(tweet_keys_from_urls(urls))
        if xhit:
            key, entry = xhit
            return DedupDecision(True, "xmon", {
                "matched_key": key,
                "matched_by": entry.get("by", ""),
                "matched_ts": entry.get("ts", ""),
            })

    return DedupDecision(False)
