from __future__ import annotations
import re

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_CDN_BLACKLIST = [
    "snsvideo.", "vweixinf.tc.qq.com", ".wx.qq.com/cgi-bin/", ".wechat.com/110",
    "emoji", "sticker", "/cdn/", "mmsns.qpic.cn",
]
_INVITE_CONTEXT_RE = re.compile(
    r"(?:邀请码|推荐码|invite\s*code|referral|邀请\s*[:：])\s*[:：]?\s*([A-Za-z0-9]{5,12})",
    re.IGNORECASE,
)
_MD5_RE = re.compile(r"<(?:emoticon|cdnthumb|file)?md5>([0-9a-fA-F]{32})</")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def extract_urls(text: str) -> list[str]:
    out = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,，。;：:")
        if any(b in u for b in _CDN_BLACKLIST):
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
