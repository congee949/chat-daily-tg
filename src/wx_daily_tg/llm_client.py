from __future__ import annotations
from dataclasses import dataclass, field
import httpx
import logging
import time

log = logging.getLogger(__name__)


@dataclass
class LLMClient:
    endpoint: str
    model: str
    api_key: str
    max_tokens: int = 8000
    timeout: float = 120.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list = field(default_factory=lambda: [5, 15, 60])

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        messages = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.retry_max_attempts:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(
                        f"{self.endpoint}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    r.raise_for_status()
                    data = r.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return content, usage
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                attempts += 1
                log.warning("llm call failed (attempt %d/%d): %s",
                            attempts, self.retry_max_attempts, e)
                if attempts >= self.retry_max_attempts:
                    break
                idx = min(attempts - 1, len(self.retry_backoff_seconds) - 1)
                time.sleep(self.retry_backoff_seconds[idx])
        assert last_exc is not None
        raise last_exc
