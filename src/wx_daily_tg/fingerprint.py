from __future__ import annotations
import re
from urllib.parse import urlparse

# Allow ) in URL, we'll strip unbalanced trailing ) separately
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Host-based CDN blacklist (matches on hostname only, not arbitrary path substrings)
_CDN_HOST_SUBSTRINGS = {
    "snsvideo.",
    "vweixinf.tc.qq.com",
    "mmsns.qpic.cn",
}
_CDN_HOST_SUFFIXES = {
    ".wx.qq.com",
    ".wechat.com",
}

# Invite code with \breferral\b to avoid URL-path false-positive,
# and negative lookahead after code to prevent truncating 13+ char codes
_INVITE_CONTEXT_RE = re.compile(
    r"(?:邀请码|推荐码|invite\s*code|\breferral\b|邀请\s*[:：])\s*[:：]?\s*"
    r"([A-Za-z0-9]{5,12})(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Any `<*md5>` XML tag with 32 hex chars. Broader than enumerated tags,
# covers keymd5, imgmd5, thumbmd5, videothumbmd5, emoticonmd5, cdnthumbmd5, filemd5, md5.
_MD5_RE = re.compile(r"<\w*md5>([0-9a-fA-F]{32})</")

# Mainland CN mobile, no surrounding digits (not surrounding word chars — dashes OK)
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def _clean_url(u: str) -> str:
    """Strip trailing punctuation and unbalanced closing parens."""
    u = u.rstrip(".,，。;：:")
    while u.endswith(")") and u.count("(") < u.count(")"):
        u = u[:-1]
    return u


def _is_cdn(u: str) -> bool:
    host = urlparse(u).netloc
    if any(h in host for h in _CDN_HOST_SUBSTRINGS):
        return True
    if any(host.endswith(s) for s in _CDN_HOST_SUFFIXES):
        return True
    return False


def extract_urls(text: str) -> list[str]:
    out = []
    for m in _URL_RE.finditer(text):
        u = _clean_url(m.group(0))
        if _is_cdn(u):
            continue
        out.append(u)
    return out


def extract_invite_codes(text: str) -> list[str]:
    return [m.group(1) for m in _INVITE_CONTEXT_RE.finditer(text)]


def extract_md5s(text: str) -> list[str]:
    return [m.group(1).lower() for m in _MD5_RE.finditer(text)]


def extract_phones(text: str) -> list[str]:
    return _PHONE_RE.findall(text)


def fingerprints_for(text: str) -> dict[str, list[str]]:
    """Return a dict of all fingerprint types found."""
    return {
        "urls": extract_urls(text),
        "invite_codes": extract_invite_codes(text),
        "md5s": extract_md5s(text),
        "phones": extract_phones(text),
    }
