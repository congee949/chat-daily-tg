"""Application entrypoints for channel forwarding and targeted resend."""
from __future__ import annotations

from chat_daily_tg import application as _legacy


def run(*, no_push: bool) -> int:
    return _legacy.run_channels(no_push=no_push)


def resend(spec: str) -> int:
    return _legacy.run_resend(spec)
