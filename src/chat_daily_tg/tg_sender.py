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
    for match in _BOLD_RE.finditer(text):
        if match.start() > last_end:
            parts.append(escape_html(text[last_end:match.start()]))
        parts.append(f"<b>{escape_html(match.group(1))}</b>")
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

    def _send_one(self, text: str, parse_mode: str | None = None) -> int:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    payload = text
                    if parse_mode == "MarkdownV2":
                        payload = format_markdownish_for_telegram(text)
                    elif parse_mode == "HTML":
                        payload = format_html_for_telegram(text)
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
        chunks = split_message(text)
        return [self._send_one(c, parse_mode) for c in chunks]
