from __future__ import annotations
from dataclasses import dataclass, field
import re
import httpx
import logging
import time
log = logging.getLogger(__name__)


def escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 special characters for Telegram safely."""
    specials = {
        "_", "*", "[", "]", "(", ")", "~", "`", ">",
        "#", "+", "-", "=", "|", "{", "}", ".", "!",
    }
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch in specials:
            out.append(f"\\{ch}")
        else:
            out.append(ch)
    return "".join(out)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*-\s+(.+)$")
# Matches an already-built <a href="…">…</a> link (group 1) OR a **bold** span (group 2).
# Order matters: links are produced upstream by post_process and must survive verbatim.
_LINK_OR_BOLD_RE = re.compile(r'(<a href="[^"]*">.*?</a>)|\*\*(.+?)\*\*')


def escape_html(text: str) -> str:
    """Escape the three characters Telegram HTML parse mode treats as special."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_html_for_telegram(text: str) -> str:
    """Convert simple markdown-ish summary text into Telegram-safe HTML.

    Supports:
    - `### Title` → `<b>Title</b>`
    - `- item` → `• item`
    - `**word**` → `<b>word</b>`
    Everything else is HTML-escaped. Pipes, dots, parens, hyphens pass through
    unchanged (unlike MarkdownV2), so pipe-separated lists stay readable.
    """
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            out_lines.append("")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            out_lines.append(f"<b>{_format_inline_html(heading.group(1).strip())}</b>")
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            out_lines.append(f"• {_format_inline_html(bullet.group(1).strip())}")
            continue

        out_lines.append(_format_inline_html(line))
    return "\n".join(out_lines)


def _format_inline_html(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in _LINK_OR_BOLD_RE.finditer(text):
        if match.start() > last_end:
            parts.append(escape_html(text[last_end:match.start()]))
        if match.group(1) is not None:
            # Already-built <a href> link from post_process: emit verbatim so it is
            # not re-escaped into broken literal markup (the LNK-1 double-escape bug).
            parts.append(match.group(1))
        else:
            parts.append(f"<b>{escape_html(match.group(2))}</b>")
        last_end = match.end()
    if last_end < len(text):
        parts.append(escape_html(text[last_end:]))
    return "".join(parts)


def format_markdownish_for_telegram(text: str) -> str:
    """Convert simple markdown-ish summary text into Telegram-safe MarkdownV2.

    Supports:
    - headings like `### Title` -> `*Title*`
    - bullet lines like `- item` -> `• item`
    - bold spans like `**word**` -> `*word*`
    Everything else is escaped so arbitrary content remains safe.
    """
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            out_lines.append("")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            out_lines.append(f"*{_format_inline_markdownish(heading.group(1).strip())}*")
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            out_lines.append(f"• {_format_inline_markdownish(bullet.group(1).strip())}")
            continue

        out_lines.append(_format_inline_markdownish(line))
    return "\n".join(out_lines)


def _format_inline_markdownish(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > last_end:
            parts.append(escape_markdown_v2(text[last_end:match.start()]))
        parts.append(f"*{escape_markdown_v2(match.group(1))}*")
        last_end = match.end()
    if last_end < len(text):
        parts.append(escape_markdown_v2(text[last_end:]))
    return "".join(parts)


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into <=limit chunks, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass
class TelegramSender:
    bot_token: str
    chat_id: str
    timeout: float = 30.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list = field(default_factory=lambda: [5, 15, 60])

    def _send_one(self, payload: str, parse_mode: str | None = None) -> int:
        """Send a single, already-formatted payload chunk. No further formatting here."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    data = {"chat_id": self.chat_id, "text": payload}
                    if parse_mode is not None:
                        data["parse_mode"] = parse_mode
                    r = c.post(url, data=data)
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RuntimeError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg send failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc

    def send(self, text: str, parse_mode: str | None = None) -> list[int]:
        # Format FIRST, then split, so the chunk length reflects the actual payload
        # sent to Telegram. Splitting raw text let HTML expansion (&->&amp;, <b> tags)
        # push a chunk over the 4096 hard limit and 400 the whole push (CHUNK-1).
        if parse_mode == "MarkdownV2":
            payload = format_markdownish_for_telegram(text)
        elif parse_mode == "HTML":
            payload = format_html_for_telegram(text)
        else:
            payload = text
        # format_*_for_telegram keeps every tag pair within a single line, so splitting
        # on newline boundaries never cuts a tag. 3900 leaves margin under 4096.
        chunks = split_message(payload, limit=3900)
        return [self._send_one(c, parse_mode) for c in chunks]

    def send_photo(self, photo_path, caption: str = "", parse_mode: str | None = None) -> int:
        """Send a photo via sendPhoto. Caption is hard-capped at Telegram's 1024 limit.

        Mirrors _send_one's retry/backoff. Used by the optional daily-card image output;
        callers wrap this in try/except and fall back to the text send() on any failure.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    with open(photo_path, "rb") as fh:
                        files = {"photo": fh}
                        data = {"chat_id": self.chat_id, "caption": caption[:1024]}
                        if parse_mode is not None:
                            data["parse_mode"] = parse_mode
                        r = c.post(url, data=data, files=files)
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RuntimeError, OSError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg sendPhoto failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc
