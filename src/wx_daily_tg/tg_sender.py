from __future__ import annotations
from dataclasses import dataclass, field
import httpx
import logging
import time

log = logging.getLogger(__name__)


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
        data = {"chat_id": self.chat_id, "text": text}
        if parse_mode is not None:
            data["parse_mode"] = parse_mode

        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with httpx.Client(timeout=self.timeout) as c:
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
