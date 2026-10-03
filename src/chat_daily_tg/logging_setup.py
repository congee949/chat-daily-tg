from __future__ import annotations
import logging
import re
from pathlib import Path

# Telegram bot tokens look like 1234567890:AA... — they leak into logs when an
# httpx error stringifies the full sendMessage URL. Redact at the formatter level so
# both the message AND the exception traceback are scrubbed (SEC-1).
# No \b anchors: the token is usually embedded as ".../bot8307…:.../" where "bot"
# abuts the digits, so a leading word boundary would never match.
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
# Hosted LLM / Google / OpenAI style secrets that show up in Authorization headers
# or query strings when httpx dumps the request.
_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_COOKIE_RE = re.compile(r"(?i)(cookie\s*[:=]\s*)([^\r\n]+)")
_REDACTED_TG = "<REDACTED_TG_TOKEN>"
_REDACTED = "<REDACTED_SECRET>"


def redact(text: str) -> str:
    """Mask common secrets in arbitrary text (notifications, alerts, logs)."""
    text = _TOKEN_RE.sub(_REDACTED_TG, text)
    text = _BEARER_RE.sub(r"\1" + _REDACTED, text)
    text = _SK_RE.sub(_REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(_REDACTED, text)
    text = _COOKIE_RE.sub(r"\1" + _REDACTED, text)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _SafeFileHandler(logging.FileHandler):
    """File handler that never raises from emit (ENOSPC must not abort a digest)."""

    def handleError(self, record: logging.LogRecord) -> None:
        return


def configure_logging(log_file: Path, level: int = logging.INFO) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    formatter = _RedactingFormatter(fmt)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(_SafeFileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
