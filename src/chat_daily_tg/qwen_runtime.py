"""Borrow the existing local Qwen runtime for a channel run."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
LABEL = "com.hermes.qwen-vl-runtime"


def _launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True,
                          text=True, timeout=10)


def _health(endpoint, api_key, *, timeout=1.0):
    try:
        response = httpx.get(endpoint.rstrip("/") + "/health",
                             headers={"Authorization": "Bearer " + api_key},
                             trust_env=False, timeout=timeout)
        return response.status_code == 200 and response.json().get("ready") is True
    except (httpx.HTTPError, ValueError):
        return False


def _wait_for_unload(target, *, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _launchctl("print", target).returncode != 0:
            return True
        time.sleep(0.25)
    return False


def _night_window():
    # Same default interval as HermesMacMemory's window controller.
    return 1 <= datetime.now(ZoneInfo("Asia/Shanghai")).hour < 6


@contextmanager
def channel_runtime(cfg, *, no_push=False):
    topic = cfg.sources.telegram.dedup.topic
    em = cfg.models.embedding if cfg.models else None
    if (no_push or not topic.enabled or not getattr(topic, "qwen_runtime_on_demand", False)
            or em is None or not em.enabled or em.provider != "openai"
            or em.endpoint.rstrip("/") != "http://127.0.0.1:8790/v1"):
        yield
        return
    api_key = os.environ.get(em.api_key_env, "")
    target = f"gui/{os.getuid()}/{LABEL}"
    plist_path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    owned = False
    lock = None
    try:
        # Serialize lifecycle changes by channel jobs on this host.
        lock_path = Path(tempfile.gettempdir()) / f"chatdaily-qwen-runtime-{os.getuid()}.lock"
        lock = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not _health(em.endpoint, api_key):
            service = _launchctl("print", target)
            loaded = service.returncode == 0
            if loaded and "state = SIGTERMed" in getattr(service, "stdout", ""):
                if not _wait_for_unload(target):
                    raise TimeoutError("Qwen stop deadline")
                loaded = False
            if not loaded:
                if plist_path.is_symlink() or plist_path.stat().st_uid != os.getuid():
                    raise ValueError("unsafe Qwen runtime plist")
                plist = plistlib.loads(plist_path.read_bytes())
                if plist.get("Label") != LABEL:
                    raise ValueError("unexpected Qwen runtime label")
                result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
                if result.returncode != 0:
                    raise RuntimeError("Qwen bootstrap failed")
                owned = True
            else:
                # Never restart an in-flight model request.
                _launchctl("kickstart", target)
            startup_timeout = float(getattr(topic, "qwen_runtime_start_timeout_seconds", 180.0))
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if _health(em.endpoint, api_key):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("Qwen startup deadline")
        log.info("L2 Qwen runtime ready on_demand=%s", owned)
    except Exception as exc:
        log.warning("L2 Qwen startup unavailable error_type=%s", type(exc).__name__)
    try:
        yield
    finally:
        try:
            if owned and not _night_window():
                result = _launchctl("bootout", target)
                unloaded = _wait_for_unload(target)
                log.info("L2 Qwen on-demand runtime release exit=%s unloaded=%s",
                         result.returncode, unloaded)
        except Exception as exc:
            log.warning("L2 Qwen release failed error_type=%s", type(exc).__name__)
        finally:
            if lock is not None:
                lock.close()
