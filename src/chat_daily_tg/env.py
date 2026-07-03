from __future__ import annotations

from pathlib import Path
import os


def scrub_socks_proxy_env() -> None:
    """Drop ALL_PROXY/all_proxy unconditionally (any scheme).

    This pipeline's intended proxy config never includes ALL_PROXY, so the pop
    doesn't bother checking the scheme.

    Shadowrocket writes ALL_PROXY=socks5://… into the launchd user environment
    (launchctl setenv) and login shells. httpx without the socksio extra raises
    ImportError at Client() construction — before NO_PROXY is even consulted —
    which killed all three launchd jobs on 2026-07-03. HTTP(S)_PROXY stays: the
    guard scripts export the working http proxy explicitly.
    """
    for key in ("ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)


def load_env_file(path: Path, *, override: bool = False) -> None:
    """Load simple KEY=VALUE lines from a local env file."""
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if not override and key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
