from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
from wx_daily_tg.fingerprint import fingerprints_for


SHORT_TEXT_THRESHOLD = 30


@dataclass(frozen=True)
class DedupKey:
    kind: str      # "url" | "invite_code" | "md5" | "phone" | "content_hash"
    value: str


@dataclass(frozen=True)
class DupeGroup:
    key: DedupKey
    messages: list[dict]


def _content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def find_cross_group_dupes(messages: list[dict]) -> list[DupeGroup]:
    """Return list of DupeGroup where each group has messages from ≥2 distinct groups.

    Input message dicts must have keys: group, sender, time, content.
    """
    key_to_msgs: dict[DedupKey, list[dict]] = {}

    def _add(key: DedupKey, msg: dict):
        key_to_msgs.setdefault(key, []).append(msg)

    for m in messages:
        content = m.get("content", "")
        fps = fingerprints_for(content)
        for url in fps["urls"]:
            _add(DedupKey("url", url), m)
        for code in fps["invite_codes"]:
            _add(DedupKey("invite_code", code), m)
        for md5 in fps["md5s"]:
            _add(DedupKey("md5", md5), m)
        for phone in fps["phones"]:
            _add(DedupKey("phone", phone), m)
        if len(content) > SHORT_TEXT_THRESHOLD:
            _add(DedupKey("content_hash", _content_hash(content)), m)

    out: list[DupeGroup] = []
    for key, msgs in key_to_msgs.items():
        groups = {m["group"] for m in msgs}
        if len(groups) >= 2:
            out.append(DupeGroup(key=key, messages=msgs))
    return out
