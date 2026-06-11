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
_REDACTED = "<REDACTED_TG_TOKEN>"


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _TOKEN_RE.sub(_REDACTED, super().format(record))


def configure_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    formatter = _RedactingFormatter(fmt)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
