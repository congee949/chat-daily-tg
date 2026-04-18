from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


WX_BINARY = shutil.which("wx") or "/opt/homebrew/bin/wx"


@dataclass(frozen=True)
class ExportResult:
    group_name: str
    out_path: Path
    message_count: int


def export_group(
    group_name: str,
    since: str,
    until: str,
    out_path: Path,
    limit: int = 10000,
) -> ExportResult:
    """Run `wx export <group>` for a single day.

    Raises RuntimeError on non-zero exit.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        WX_BINARY,
        "export",
        group_name,
        "--since", since,
        "--until", until,
        "--limit", str(limit),
        "--format", "markdown",
        "-o", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"wx export failed: {proc.stderr or proc.stdout}")
    # Parse "已导出 N 条消息" from stdout
    m = re.search(r"已导出\s+(\d+)\s+条消息", proc.stdout)
    count = int(m.group(1)) if m else 0
    return ExportResult(group_name=group_name, out_path=out_path, message_count=count)
