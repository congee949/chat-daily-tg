"""Ephemeral Cloudflare KV image relay for sendRichMessage.

Rich messages embed media by public https URL only — Telegram fetches the URL
server-side DURING the sendRichMessage call and re-hosts the image as its own
PhotoSize copies, so the source URL can (and should) die right after the call
returns. Flow: upload_image -> sendRichMessage -> delete_image; the KV
expiration TTL is the backstop if the delete is never reached.
"""
from __future__ import annotations

import logging
import os
import secrets

import httpx

from chat_daily_tg.config import ImgRelay

log = logging.getLogger(__name__)

_API = "https://api.cloudflare.com/client/v4"


def _kv_url(cfg: ImgRelay, key: str) -> str:
    return (f"{_API}/accounts/{cfg.account_id}/storage/kv/namespaces/"
            f"{cfg.namespace_id}/values/{key}")


def upload_image(cfg: ImgRelay, image_path: str) -> str:
    """Upload the image to KV under an unguessable key; returns the public URL.

    Raises on any failure — the caller falls back to the non-rich push path.
    """
    token = os.environ[cfg.api_token_env]
    key = f"{secrets.token_hex(24)}.jpg"
    with open(image_path, "rb") as f:
        data = f.read()
    with httpx.Client(timeout=60) as c:
        r = c.put(
            _kv_url(cfg, key),
            params={"expiration_ttl": str(cfg.ttl_seconds)},
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream"},
            content=data,
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"KV put failed: {body.get('errors')}")
    return f"{cfg.worker_base.rstrip('/')}/{key}"


def delete_image(cfg: ImgRelay, url: str) -> None:
    """Best-effort immediate KV delete (TTL still covers failures); never raises."""
    key = url.rsplit("/", 1)[-1]
    try:
        token = os.environ[cfg.api_token_env]
        with httpx.Client(timeout=30) as c:
            c.delete(_kv_url(cfg, key),
                     headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        log.warning("KV delete failed (TTL will expire it): %s", e)
