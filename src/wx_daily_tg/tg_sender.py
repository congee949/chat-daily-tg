from __future__ import annotations
from dataclasses import dataclass
import httpx


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into <=limit chunks, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to split on last newline within limit
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

    def send(self, text: str, parse_mode: str | None = None) -> list[int]:
        """Send text (splitting if needed). Returns list of message_ids."""
        chunks = split_message(text)
        ids: list[int] = []
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        with httpx.Client(timeout=self.timeout) as c:
            for chunk in chunks:
                data = {"chat_id": self.chat_id, "text": chunk}
                if parse_mode is not None:
                    data["parse_mode"] = parse_mode
                r = c.post(url, data=data)
                r.raise_for_status()
                body = r.json()
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API error: {body}")
                ids.append(body["result"]["message_id"])
        return ids
