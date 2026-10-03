from __future__ import annotations

import re


_LLM_RISK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"护照",
        r"签证",
        r"美签",
        r"留學打工",
        r"留学打工",
        r"台湾护照",
        r"豬肝护照",
        r"猪肝护照",
    ]
]

_CREDENTIAL_REDACTION = "[凭据已脱敏]"
_PHONE_REDACTION = "[手机号已脱敏]"

# These patterns operate on source text immediately before it is put into an
# LLM prompt.  Prefer labelled/header forms and well-known prefixes over a
# broad "long random string" heuristic: message IDs, timestamps and error
# codes are useful diagnostic evidence and must remain readable.
_COOKIE_RE = re.compile(
    r"(?im)(?P<prefix>[\"']?(?:set-cookie|cookie)[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?authorization[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,;}\]\r\n]+)"
)
_KEYED_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:"
    r"api[_ -]?key|apikey|x-api-key|x-goog-api-key|"
    r"access[_ -]?token|secret[_ -]?key"
    r")[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)"
)
_BEARER_RE = re.compile(
    r"(?i)(?P<prefix>\bbearer\s+)(?P<secret>[A-Za-z0-9._~+/=-]{8,})"
)
_PREFIXED_SECRET_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{10,}\b",
        r"\bAIza[0-9A-Za-z_-]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    ]
]
_CN_MOBILE_RE = re.compile(
    r"(?<!\d)(?:(?:\+?86)[ -]?)?1[3-9]\d(?:[ -]?\d){8}(?!\d)"
)
_INTERNATIONAL_PHONE_RE = re.compile(
    r"(?<!\w)\+\d[\d ()-]{7,}\d(?!\w)"
)


def _mask_structured_value(match: re.Match[str]) -> str:
    """Keep a header/field and its quoting while removing its value."""
    value = match.group("value")
    quote = value[0] if value[:1] in {'"', "'"} and value[-1:] == value[:1] else ""
    inner = value[1:-1] if quote else value

    # Retaining the auth scheme is useful when diagnosing a malformed request.
    scheme = ""
    if match.group("prefix").lower().strip(" \t\"'").startswith("authorization"):
        scheme_match = re.match(r"(?i)(bearer|basic|token)\s+", inner)
        if scheme_match:
            scheme = scheme_match.group(0)
    return f"{match.group('prefix')}{quote}{scheme}{_CREDENTIAL_REDACTION}{quote}"


def _mask_international_phone(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = sum(char.isdigit() for char in candidate)
    # E.164 numbers contain at most 15 digits.  The lower bound avoids masking
    # short error codes written with a leading plus sign.
    return _PHONE_REDACTION if 10 <= digits <= 15 else candidate


def sanitize_for_llm(text: str) -> str:
    """Redact phrases that commonly trip hosted LLM content filters.

    This only changes the model prompt. Raw source exports remain archived.
    """
    out = text
    for pattern in _LLM_RISK_PATTERNS:
        out = pattern.sub("[已脱敏]", out)
    out = _COOKIE_RE.sub(_mask_structured_value, out)
    out = _AUTHORIZATION_RE.sub(_mask_structured_value, out)
    out = _KEYED_SECRET_RE.sub(_mask_structured_value, out)
    out = _BEARER_RE.sub(r"\g<prefix>" + _CREDENTIAL_REDACTION, out)
    for pattern in _PREFIXED_SECRET_PATTERNS:
        out = pattern.sub(_CREDENTIAL_REDACTION, out)
    out = _CN_MOBILE_RE.sub(_PHONE_REDACTION, out)
    out = _INTERNATIONAL_PHONE_RE.sub(_mask_international_phone, out)
    return out
