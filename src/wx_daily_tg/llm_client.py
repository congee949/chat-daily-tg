from __future__ import annotations
from dataclasses import dataclass
import httpx


@dataclass
class LLMClient:
    endpoint: str
    model: str
    api_key: str
    max_tokens: int = 8000
    timeout: float = 120.0

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        """Single-turn completion. Returns (content, usage_dict)."""
        messages = []
        if system:
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
