"""Decode WeChat's proprietary wxgf (WXAM) image container to standard JPEG.

wxgf wraps HEVC video bitstreams (static images = one dominant partition,
animated stickers = alternating mask/anime partitions). Layout, per public
reverse engineering (sarv.blog/posts/wxam/, sjzar/chatlog dat2img/wxgf.go):

    magic "wxgf" (4B) | header_len (1B) | version (2B) | width (2B, BE)
    | height (2B, BE) | bit-packed args ... | partitions

Each partition is ``[4B big-endian length][Annex-B start code]...`` where the
length counts from the start code. The payload is a raw HEVC Annex-B stream
that ffmpeg decodes directly, so full header parsing is unnecessary.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

log = logging.getLogger(__name__)

WXGF_MAGIC = b"wxgf"

_FFMPEG_FALLBACK = Path("/opt/homebrew/bin/ffmpeg")
_FFMPEG_TIMEOUT = 20.0
_START_CODES = (b"\x00\x00\x00\x01", b"\x00\x00\x01")
# Below this largest-partition share of the file, treat as animated (chatlog's MinRatio).
_ANIME_MAX_RATIO = 0.6
# mjpeg qscale 3 ≈ JPEG quality ~90.
_MJPEG_QSCALE = "3"


def is_wxgf(data: bytes) -> bool:
    return data[:4] == WXGF_MAGIC


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if _FFMPEG_FALLBACK.exists():
        return str(_FFMPEG_FALLBACK)
    return None


def _find_partitions(data: bytes) -> list[tuple[int, int]]:
    """Return (offset, size) of length-prefixed HEVC partitions after the header."""
    if len(data) < 6:
        return []
    header_len = data[4]
    if header_len >= len(data):
        return []
    for pattern in _START_CODES:
        parts: list[tuple[int, int]] = []
        offset = header_len
        while offset < len(data):
            idx = data.find(pattern, offset)
            if idx == -1:
                break
            if idx < 4:
                offset = idx + 1
                continue
            (length,) = struct.unpack(">I", data[idx - 4 : idx])
            if length <= 0 or idx + length > len(data):
                offset = idx + 1
                continue
            parts.append((idx, length))
            offset = idx + length
        if parts:
            return parts
    return []


def _payload_candidates(data: bytes) -> list[bytes]:
    """HEVC byte-stream slices to try, most promising first.

    Static images: the single dominant partition. Animated wxgf alternates
    mask/anime partitions (even=mask, odd=anime) and each stream's first frame
    carries its own parameter sets, so partition 1 yields the first real frame.
    Last resort: everything from the first start code, for files where the
    length-prefix convention does not hold.
    """
    candidates: list[bytes] = []
    parts = _find_partitions(data)
    if parts:
        largest = max(parts, key=lambda p: p[1])
        if len(parts) > 1 and largest[1] / len(data) < _ANIME_MAX_RATIO:
            off, size = parts[1]
            candidates.append(data[off : off + size])
        off, size = largest
        candidates.append(data[off : off + size])
    search_from = data[4] if len(data) > 4 else 0
    for pattern in _START_CODES:
        idx = data.find(pattern, search_from)
        if idx != -1:
            candidates.append(data[idx:])
            break
    unique: list[bytes] = []
    for cand in candidates:
        if cand not in unique:
            unique.append(cand)
    return unique


def _ffmpeg_first_frame(
    ffmpeg: str, payload: bytes, input_format: str, out_path: Path
) -> tuple[bool, str]:
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", input_format, "-i", "pipe:0",
        "-frames:v", "1", "-c:v", "mjpeg", "-q:v", _MJPEG_QSCALE,
        "-pix_fmt", "yuvj420p", "-f", "image2", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, input=payload, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", "replace").strip()
    if not out_path.exists() or out_path.stat().st_size == 0:
        return False, "ffmpeg wrote no output"
    return True, ""


def _pil_openable(path: Path) -> bool:
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as img:
            img.load()
        return True
    except Exception:
        return False


def decode_wxgf(src: Path, dst: Path) -> bool:
    """Transcode a wxgf container at src into a PIL-openable JPEG at dst.

    Returns False (leaving dst untouched — tmp file + atomic replace) when src
    is not wxgf, ffmpeg is unavailable, or every decode attempt fails. src and
    dst may be the same path; the original is only replaced on success.
    """
    try:
        data = src.read_bytes()
    except OSError as e:
        log.debug("wxgf decode cannot read %s: %s", src, e)
        return False
    if not is_wxgf(data):
        return False
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        log.debug("wxgf decode skipped for %s: ffmpeg not available", src)
        return False
    candidates = _payload_candidates(data)
    if not candidates:
        log.debug("wxgf decode failed for %s: no HEVC start code found", src)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.stem}.wxgf.", suffix=".jpg", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        for cand_idx, payload in enumerate(candidates):
            for input_format in ("hevc", "h264"):
                ok, err = _ffmpeg_first_frame(ffmpeg, payload, input_format, tmp)
                if not ok:
                    log.debug(
                        "wxgf decode attempt cand=%d fmt=%s failed for %s: %s",
                        cand_idx, input_format, src, err,
                    )
                    continue
                if not _pil_openable(tmp):
                    log.debug(
                        "wxgf decode attempt cand=%d fmt=%s for %s: output not PIL-openable",
                        cand_idx, input_format, src,
                    )
                    continue
                os.replace(tmp, dst)
                return True
        return False
    finally:
        tmp.unlink(missing_ok=True)
