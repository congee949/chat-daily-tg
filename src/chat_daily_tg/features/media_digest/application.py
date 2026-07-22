"""Application entrypoints for Bilibili and YouTube digests."""
from __future__ import annotations

from chat_daily_tg import application as _legacy


def run_bilibili(*, no_push: bool) -> int:
    return _legacy.run_bilibili(no_push=no_push)


def run_youtube(*, no_push: bool) -> int:
    return _legacy.run_youtube(no_push=no_push)
