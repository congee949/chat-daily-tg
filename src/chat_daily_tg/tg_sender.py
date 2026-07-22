from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import httpx
import logging
import time
log = logging.getLogger(__name__)


# Telegram sendXxx method + multipart field name per media kind.
_MEDIA_METHOD = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "audio": ("sendAudio", "audio"),
    "document": ("sendDocument", "document"),
}
# media_group item type per kind (photos/videos can share one group).
_GROUP_TYPE = {"photo": "photo", "video": "video", "audio": "audio", "document": "document"}


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


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain(text: str) -> str:
    """Strip HTML tags and unescape entities — used to degrade a 400'd card to plain text."""
    stripped = _TAG_RE.sub("", text)
    return stripped.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


_CAPTION_LIMIT = 1024


def _safe_caption(caption: str) -> tuple[str, str | None]:
    """Telegram counts the 1024-char caption limit on VISIBLE text (after entity
    parsing), so a well-formed HTML caption within the visible limit must pass
    through uncut — slicing raw HTML can cut inside a tag and 400 on every retry.
    An over-limit caption degrades to truncated plain text with no parse_mode
    (unescaped '<' would 400 the HTML parser). Returns (caption, parse_mode)."""
    visible = _html_to_plain(caption)
    if len(visible) <= _CAPTION_LIMIT:
        return caption, "HTML"
    return visible[:_CAPTION_LIMIT], None


def _retry_after(r: "httpx.Response") -> float:
    """Seconds to wait on a 429, clamped to [1, 30]."""
    try:
        return min(max(int(r.json()["parameters"]["retry_after"]), 1), 30)
    except Exception:
        return 3


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
    message_thread_id: int | None = None
    timeout: float = 30.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list = field(default_factory=lambda: [5, 15, 60])
    client: httpx.Client | None = field(default=None, repr=False)
    _owned_client_context: httpx.Client | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def _http_client(self) -> httpx.Client:
        """Return this sender's reusable client without changing its route policy.

        Telegram intentionally keeps httpx's default ``trust_env=True`` so the
        guarded jobs use their configured HTTP(S) proxy.  The client is created
        lazily because ``--no-push`` must not touch the network stack at all.
        """
        if self._closed:
            raise RuntimeError("TelegramSender is closed")
        if self.client is None:
            self._owned_client_context = httpx.Client(timeout=self.timeout)
            self.client = self._owned_client_context.__enter__()
        return self.client

    @contextmanager
    def _client_session(self):
        yield self._http_client()

    def close(self) -> None:
        """Release an internally-created client pool (safe to call repeatedly)."""
        if self._closed:
            return
        self._closed = True
        if self._owned_client_context is not None:
            self._owned_client_context.__exit__(None, None, None)
        self._owned_client_context = None
        self.client = None

    def __enter__(self) -> "TelegramSender":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - legacy callers may not close
        try:
            self.close()
        except Exception:
            pass

    def _send_one(self, payload: str, parse_mode: str | None = None) -> int:
        """Send a single, already-formatted payload chunk. No further formatting here."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    data = {"chat_id": self.chat_id, "text": payload}
                    if parse_mode is not None:
                        data["parse_mode"] = parse_mode
                    if self.message_thread_id is not None:
                        data["message_thread_id"] = self.message_thread_id
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

    def send(self, text: str, parse_mode: str | None = None,
             *, state_path=None) -> list[int]:
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
        if state_path is None or len(chunks) <= 1:
            return [self._send_one(c, parse_mode) for c in chunks]
        return self._send_resumable(chunks, parse_mode, state_path)

    def _send_resumable(self, chunks: list[str], parse_mode: str | None, state_path) -> list[int]:
        """Send chunks, persisting per-chunk progress so a same-day catch-up rerun
        resumes instead of re-sending the first half (review finding #42). Resume is
        gated on a payload hash: if the (non-deterministic) report text changed
        between runs, restart from the top rather than splice mismatched halves."""
        from hashlib import sha256
        from pathlib import Path
        sp = Path(state_path)
        digest = sha256("\x00".join(chunks).encode("utf-8")).hexdigest()
        resume_from = 0
        try:
            prev = json.loads(sp.read_text(encoding="utf-8"))
            if prev.get("hash") == digest and isinstance(prev.get("sent"), int):
                resume_from = min(prev["sent"], len(chunks))
        except (OSError, ValueError):
            pass
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            if i < resume_from:
                continue
            ids.append(self._send_one(chunk, parse_mode))
            try:
                sp.write_text(json.dumps({"hash": digest, "sent": i + 1}), encoding="utf-8")
            except OSError:
                pass
        return ids

    def send_card(self, text_html: str, *, link: str | None = None,
                  button: tuple[str, str] | None = None) -> list[int]:
        """Send a verbatim channel message as an X-Monitor-style card.

        `text_html` is already-built Telegram HTML (callers escape their own content).
        For a public channel `link` (a t.me/<username>/<id> URL) enables Telegram's
        rich link-preview card; pass link=None for a private channel to send plain text
        with no preview. `button` is an optional (text, url) inline-keyboard URL button
        attached to the last chunk.

        Long messages are split on newline boundaries; only the LAST chunk carries the
        preview so the card renders once at the end. On a 400 (usually an HTML parse
        error) the chunk degrades to plain text once and resends, so a single bad
        message never drops the whole push.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        chunks = split_message(text_html, limit=3900)
        msg_ids: list[int] = []
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            payload: dict = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML"}
            if button is not None and is_last:
                payload["reply_markup"] = {"inline_keyboard": [[
                    {"text": button[0], "url": button[1]},
                ]]}
            if self.message_thread_id is not None:
                payload["message_thread_id"] = self.message_thread_id
            if link and is_last:
                payload["link_preview_options"] = {
                    "url": link, "is_disabled": False, "prefer_large_media": True,
                }
            else:
                payload["link_preview_options"] = {"is_disabled": True}
            msg_ids.append(self._post_json(url, payload))
        return msg_ids

    def send_rich_message(
        self,
        *,
        markdown: str,
        media: list[tuple[str, str, str]] | None = None,
    ) -> int:
        """Send a Bot API rich message (sendRichMessage): one message that mixes
        text blocks and media blocks.

        `media` contains `(id, local_path, kind)` entries. Bot API 10.2 maps each
        id to a `tg://photo?id=...` / video / audio reference and uploads the file
        in the same multipart request, so callers don't need a public relay URL.

        A 400 raises IMMEDIATELY (bad markdown / unfetchable image URL is
        deterministic — the caller falls back to the classic text+photo push);
        429 waits per retry_after; transport errors retry with backoff."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendRichMessage"
        media = media or []
        rich_message: dict = {"markdown": markdown}
        if media:
            rich_message["media"] = [
                {
                    "id": media_id,
                    "media": {
                        "type": kind,
                        "media": f"attach://rich_media_{index}",
                    },
                }
                for index, (media_id, _path, kind) in enumerate(media)
            ]
        last_exc: Exception | None = None
        attempts = 0
        rl_hits = 0
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    if media:
                        handles = []
                        try:
                            files = {}
                            for index, (_media_id, path, _kind) in enumerate(media):
                                fh = open(path, "rb")
                                handles.append(fh)
                                files[f"rich_media_{index}"] = (Path(path).name, fh)
                            data: dict = {
                                "chat_id": self.chat_id,
                                "rich_message": json.dumps(rich_message, ensure_ascii=False),
                            }
                            if self.message_thread_id is not None:
                                data["message_thread_id"] = self.message_thread_id
                            r = c.post(url, data=data, files=files)
                        finally:
                            for fh in handles:
                                fh.close()
                    else:
                        payload: dict = {
                            "chat_id": self.chat_id,
                            "rich_message": rich_message,
                        }
                        if self.message_thread_id is not None:
                            payload["message_thread_id"] = self.message_thread_id
                        r = c.post(url, json=payload)
                    if r.status_code == 429:
                        rl_hits += 1
                        if rl_hits >= self.retry_max_attempts:
                            raise RuntimeError(f"Telegram 429 rate limit: gave up after {rl_hits} waits")
                        time.sleep(_retry_after(r))
                        continue
                    if r.status_code == 400:
                        raise RuntimeError(f"sendRichMessage 400: {r.text[:200]}")
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except (httpx.TimeoutException, httpx.ConnectError, OSError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg sendRichMessage transport failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc

    def _post_json(self, url: str, payload: dict) -> int:
        """POST a JSON sendMessage payload with retry/backoff. Honors 429 retry_after
        and degrades to plain text once on a 400 parse error."""
        last_exc: Exception | None = None
        attempts = 0
        rl_hits = 0
        degraded = False
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    r = c.post(url, json=payload)
                    if r.status_code == 429:
                        rl_hits += 1
                        if rl_hits >= self.retry_max_attempts:
                            last_exc = RuntimeError(f"Telegram 429 rate limit: gave up after {rl_hits} waits")
                            break
                        time.sleep(_retry_after(r))
                        continue
                    if r.status_code == 400 and payload.get("parse_mode") and not degraded:
                        degraded = True
                        payload = dict(payload)
                        payload["text"] = _html_to_plain(payload["text"])
                        payload.pop("parse_mode", None)
                        continue
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RuntimeError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg send_card failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc

    def send_photo(self, photo_path, caption: str = "", parse_mode: str | None = None,
                   button: tuple[str, str] | None = None) -> int:
        """Send a photo via sendPhoto. Caption is hard-capped at Telegram's 1024 limit.

        `button` is an optional (text, url) pair rendered as a single inline-keyboard
        URL button under the card — a bigger tap target than an <a> link in the caption.

        Mirrors _send_one's retry/backoff. Used by the optional daily-card image output;
        callers wrap this in try/except and fall back to the text send() on any failure.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    with open(photo_path, "rb") as fh:
                        files = {"photo": fh}
                        data = {"chat_id": self.chat_id}
                        if self.message_thread_id is not None:
                            data["message_thread_id"] = self.message_thread_id
                        if button is not None:
                            # multipart form field — reply_markup must be JSON-encoded
                            data["reply_markup"] = json.dumps({"inline_keyboard": [[
                                {"text": button[0], "url": button[1]},
                            ]]})
                        if caption:
                            data["caption"] = caption[:1024]
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

    def send_media(self, file_path: str, kind: str, *, caption: str = "") -> int:
        """Upload a single media file (sendPhoto/sendVideo/sendAudio/sendDocument).

        `caption` is Telegram HTML; the 1024 limit is enforced on VISIBLE length
        (see _safe_caption). Mirrors the send_photo retry/backoff. Used for
        verbatim private-channel media."""
        method, field_name = _MEDIA_METHOD.get(kind, _MEDIA_METHOD["document"])
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        last_exc: Exception | None = None
        attempts = 0
        rl_hits = 0
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    with open(file_path, "rb") as fh:
                        files = {field_name: fh}
                        data = {"chat_id": self.chat_id}
                        if self.message_thread_id is not None:
                            data["message_thread_id"] = self.message_thread_id
                        if caption:
                            cap, cap_mode = _safe_caption(caption)
                            data["caption"] = cap
                            if cap_mode is not None:
                                data["parse_mode"] = cap_mode
                        r = c.post(url, data=data, files=files)
                    if r.status_code == 429:
                        rl_hits += 1
                        if rl_hits >= self.retry_max_attempts:
                            last_exc = RuntimeError(f"Telegram 429 rate limit: gave up after {rl_hits} waits")
                            break
                        time.sleep(_retry_after(r))
                        continue
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RuntimeError, OSError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg %s failed (attempt %d/%d): %s",
                            method, attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc

    def send_media_group(self, items: list[tuple[str, str]], *, caption: str = "") -> list[int]:
        """Send up to 10 media files as one album (sendMediaGroup).

        `items` is [(file_path, kind), …]; caption (HTML) goes on the first item.
        Media groups do not support inline buttons, so callers that need an 打开原文
        link embed it in the caption text. Mixed photo/document groups are the caller's
        responsibility to avoid (Telegram 400s on them)."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMediaGroup"
        items = items[:10]
        last_exc: Exception | None = None
        attempts = 0
        rl_hits = 0
        while attempts < self.retry_max_attempts:
            try:
                with self._client_session() as c:
                    handles = []
                    try:
                        media = []
                        files = {}
                        for i, (path, kind) in enumerate(items):
                            key = f"file{i}"
                            fh = open(path, "rb")
                            handles.append(fh)
                            files[key] = fh
                            m = {"type": _GROUP_TYPE.get(kind, "document"),
                                 "media": f"attach://{key}"}
                            if i == 0 and caption:
                                cap, cap_mode = _safe_caption(caption)
                                m["caption"] = cap
                                if cap_mode is not None:
                                    m["parse_mode"] = cap_mode
                            media.append(m)
                        data = {"chat_id": self.chat_id, "media": json.dumps(media)}
                        if self.message_thread_id is not None:
                            data["message_thread_id"] = self.message_thread_id
                        r = c.post(url, data=data, files=files)
                    finally:
                        for fh in handles:
                            fh.close()
                    if r.status_code == 429:
                        rl_hits += 1
                        if rl_hits >= self.retry_max_attempts:
                            last_exc = RuntimeError(f"Telegram 429 rate limit: gave up after {rl_hits} waits")
                            break
                        time.sleep(_retry_after(r))
                        continue
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return [m["message_id"] for m in body["result"]]
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, RuntimeError, OSError) as e:
                last_exc = e
                attempts += 1
                log.warning("tg sendMediaGroup failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc
