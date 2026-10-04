"""Local model identity for embedding and reranking clients."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()



def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))



def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()



def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



def model_revision_fingerprint(model_path: Path) -> str:
    """Fingerprint local weights without rereading multi-GB shards per query.

    Size/mtime alone is insufficient because a same-sized replacement can
    preserve mtime.  APFS inode and ctime change for replacement and in-place
    writes respectively; small model/config files are additionally hashed in
    full.  This keeps online reader startup cheap while detecting the practical
    local weight-mutation cases that would invalidate a generation.
    """
    rows: list[dict[str, Any]] = []
    for path in sorted(model_path.glob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        stat_result = path.stat()
        row: dict[str, Any] = {
            "name": path.name,
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "ctime_ns": stat_result.st_ctime_ns,
            "inode": stat_result.st_ino,
            "device": stat_result.st_dev,
        }
        if path.suffix in {".json", ".txt"} and stat_result.st_size <= 10_000_000:
            row["sha256"] = sha256_file(path)
        rows.append(row)
    if not rows:
        raise FileNotFoundError(f"embedding model directory is empty: {model_path}")
    return sha256_text(canonical_json(rows))
