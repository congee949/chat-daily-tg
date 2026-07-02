from __future__ import annotations
from dataclasses import dataclass, replace
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import time
from chat_daily_tg.archive import safe_filename
from chat_daily_tg.media import MediaCandidate, extract_wx_media_candidates

log = logging.getLogger(__name__)

WX_BINARY = shutil.which("wx") or "/opt/homebrew/bin/wx"

# Only download candidates worth vision's attention — matches vision.py's
# min_prefilter_score, so nothing downloaded here is thrown away downstream.
_MIN_DOWNLOAD_SCORE = 0.45


@dataclass(frozen=True)
class ExportResult:
    group_name: str
    out_path: Path
    message_count: int
    content: str
    media_candidates: list[MediaCandidate] | None = None


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


def _attachment_ids_by_local_id(group_name: str, since: str, until: str) -> dict[int, str]:
    """`wx attachments --json` local_id matches the local_id=NNN already parsed
    from export text — this maps that id to the opaque attachment_id `wx extract` needs."""
    cmd = [
        WX_BINARY, "attachments", group_name, "--kind", "image",
        "--since", since, "--until", until, "--json", "--limit", "10000",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        log.warning("wx attachments failed for %s: %s", group_name, proc.stderr or proc.stdout)
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("wx attachments returned non-JSON for %s", group_name)
        return {}
    return {item["local_id"]: item["attachment_id"] for item in data.get("attachments", [])}


def _download_wx_images(
    candidates: list[MediaCandidate], *, group_name: str, since: str, until: str, media_dir: Path,
) -> list[MediaCandidate]:
    """Extract score-qualifying image candidates via `wx extract`, in place.

    Only candidates already worth vision's attention are downloaded — the score comes
    from message-text keywords alone (media.py), so this needs no image data up front.
    Per-item extract failures are logged and skipped; they never abort the export.
    """
    qualifies = {
        idx for idx, c in enumerate(candidates)
        if c.media_type == "图片" and c.score >= _MIN_DOWNLOAD_SCORE and c.raw_ref
    }
    if not qualifies:
        return candidates
    by_local_id = _attachment_ids_by_local_id(group_name, since, until)
    if not by_local_id:
        return candidates
    media_dir.mkdir(parents=True, exist_ok=True)
    updated: dict[int, MediaCandidate] = {}
    for idx in qualifies:
        c = candidates[idx]
        local_id = int(c.raw_ref.split("=", 1)[1])
        attachment_id = by_local_id.get(local_id)
        if not attachment_id:
            continue
        out_path = media_dir / f"{local_id}.jpg"
        if not out_path.exists():
            proc = subprocess.run(
                [WX_BINARY, "extract", attachment_id, "-o", str(out_path)],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                log.warning("wx extract failed for local_id=%d: %s", local_id, proc.stderr or proc.stdout)
                continue
        updated[idx] = replace(c, local_path=str(out_path))
    return [updated.get(i, c) for i, c in enumerate(candidates)]


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
    # The wx daemon reports ready (contacts loaded) ~1.5s before it finishes decrypting the
    # message DB. On a cold daemon — the usual case for the unattended launchd run after the
    # Mac has been idle — the first `wx export` races that decryption and returns a non-zero
    # "找不到消息记录" exit or an empty result. Retry a few times so the daemon can finish
    # warming before an empty result is taken at face value. A genuinely quiet day still exits
    # the loop after the retries and returns 0 messages without raising.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    m = _COUNT_RE.search(proc.stdout)
    for _ in range(3):
        if proc.returncode == 0 and m and int(m.group(1)) > 0:
            break
        time.sleep(2.0)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        m = _COUNT_RE.search(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"wx export failed: {proc.stderr or proc.stdout}")
    raw = proc.stdout
    count = int(m.group(1)) if m else 0
    media_candidates = extract_wx_media_candidates(raw, group_name=group_name)
    media_dir = out_path.parent / "wx_media" / safe_filename(group_name)
    media_candidates = _download_wx_images(
        media_candidates, group_name=group_name, since=since, until=until, media_dir=media_dir,
    )
    cleaned = clean_wx_markdown(raw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(cleaned, encoding="utf-8")
    return ExportResult(
        group_name=group_name, out_path=out_path,
        message_count=count, content=cleaned,
        media_candidates=media_candidates,
    )
