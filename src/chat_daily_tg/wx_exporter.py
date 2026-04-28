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
    content: str


_TS_HEADER = r"### \d{4}-\d{2}-\d{2} \d{2}:\d{2}"


def _block(payload_re: str) -> re.Pattern[str]:
    return re.compile(rf"^{_TS_HEADER}\n\n{payload_re}\n(?:\n)?", re.MULTILINE)


# Each _BLOCK_* matches `### timestamp\n\n<payload>\n[optional blank]` and is dropped whole.
_BLOCK_PATPAT = _block(r"\[链接\][^\n]*拍了拍[^\n]*")
_BLOCK_SYSTEM = _block(r"\[系统\][^\n]*")
_BLOCK_EMPTY_MSG = _block(r"\*\*[^*]+\*\*:\s*")

# Attachment placeholders carrying only an internal local_id — no content for LLM.
# Today only [图片] emits local_id; widening covers future `wx` CLI versions.
_ATTACH_LOCAL = re.compile(
    r"\[(?:图片|视频|文件|语音|位置|动画表情|音乐|小程序|链接卡片)\]\s*local_id=\d+"
)
# Sticker/emoji placeholders: CJK ([捂脸][引用][红包]), English ([Emm][OK][Doge]),
# digits ([666]). Alnum/CJK only, ≤10 chars — keeps user-written `[短语]` safe.
_EMOJI_INLINE = re.compile(r"\[[A-Za-z0-9\u4e00-\u9fff]{1,10}\]")


def clean_wx_markdown(md: str) -> str:
    """Strip nudges, system notices, sticker/attachment placeholders, and the
    resulting empty message blocks and whitespace from wx-export markdown."""
    for pat in (_BLOCK_PATPAT, _BLOCK_SYSTEM, _ATTACH_LOCAL, _EMOJI_INLINE, _BLOCK_EMPTY_MSG):
        md = pat.sub("", md)
    md = re.sub(r"[ \t]{2,}", " ", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md


_COUNT_RE = re.compile(r"导出\s+(\d+)\s+条消息")


def export_group(
    group_name: str,
    since: str,
    until: str,
    out_path: Path,
    limit: int = 10000,
) -> ExportResult:
    """Run `wx export <group>` capturing markdown from stdout, clean, write once.

    Raises RuntimeError on non-zero exit.
    """
    cmd = [
        WX_BINARY, "export", group_name,
        "--since", since, "--until", until,
        "--limit", str(limit), "--format", "markdown",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"wx export failed: {proc.stderr or proc.stdout}")
    raw = proc.stdout
    m = _COUNT_RE.search(raw)
    count = int(m.group(1)) if m else 0
    cleaned = clean_wx_markdown(raw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(cleaned, encoding="utf-8")
    return ExportResult(
        group_name=group_name, out_path=out_path,
        message_count=count, content=cleaned,
    )
