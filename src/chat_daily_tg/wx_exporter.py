from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from chat_daily_tg.archive import safe_filename
from chat_daily_tg.media import MediaCandidate, extract_wx_media_candidates
from chat_daily_tg.wxgf import decode_wxgf, is_wxgf

log = logging.getLogger(__name__)


_WX_PLATFORM_PACKAGES = {
    "darwin-arm64": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "linux-arm64": "linux-arm64",
    "linux-x64": "linux-x64",
    "win32-x64": "win32-x64",
}


def _node_platform_key() -> str:
    """Return the platform key used by the wx-cli npm launcher.

    ``platform.machine()`` uses ``x86_64``/``aarch64`` on some systems while
    Node uses ``x64``/``arm64``.  Keep the mapping local and conservative: an
    unknown platform simply disables the optimization and uses the wrapper.
    """
    system = sys.platform
    if system == "darwin":
        system = "darwin"
    elif system.startswith("linux"):
        system = "linux"
    elif system.startswith("win"):
        system = "win32"
    else:
        return ""
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "x64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if arch is None:
        return ""
    key = f"{system}-{arch}"
    return key if key in _WX_PLATFORM_PACKAGES else ""


def _is_native_wx_binary(path: Path) -> bool:
    """Check a candidate without executing it or starting the wx daemon.

    The npm entry point is a Node script, whereas the platform package contains
    a Mach-O/ELF/PE executable.  A small magic-byte check is enough here and
    avoids adding a ``wx --version`` subprocess to every unattended run.
    """
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError:
        return False
    if magic.startswith(b"\x7fELF") or magic[:2] == b"MZ":
        return True
    # Mach-O (32/64-bit, either endian) and universal/fat binaries.
    return magic in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    }


def _read_package_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _manifest_declares_wrapper(manifest: dict[str, object], wrapper: Path) -> bool:
    """Verify that a package manifest actually owns ``wrapper``."""
    declared = manifest.get("bin")
    if isinstance(declared, str):
        entries = {"wx": declared}
    elif isinstance(declared, dict):
        raw = declared.get("wx")
        entries = {"wx": raw} if isinstance(raw, str) else {}
    else:
        entries = {}
    if not entries:
        return False
    try:
        package_root = wrapper.parent.parent
        return (package_root / entries["wx"]).resolve() == wrapper.resolve()
    except (OSError, RuntimeError):
        return False


def _native_from_node_wrapper(wrapper: Path) -> Path | None:
    """Resolve the platform package behind a standard wx-cli Node wrapper."""
    key = _node_platform_key()
    if not key:
        return None
    try:
        resolved_wrapper = wrapper.resolve()
    except (OSError, RuntimeError):
        return None

    # Find the nearest package root and require its manifest to point at the
    # exact wrapper.  This prevents accidentally replacing a custom ``wx``
    # script with an unrelated sibling binary.
    package_root: Path | None = None
    for parent in (resolved_wrapper.parent, *resolved_wrapper.parents):
        manifest = _read_package_json(parent / "package.json")
        if manifest is None:
            continue
        if _manifest_declares_wrapper(manifest, resolved_wrapper):
            package_root = parent
            break
    if package_root is None:
        return None
    manifest = _read_package_json(package_root / "package.json")
    if manifest is None:
        return None
    optional = manifest.get("optionalDependencies")
    if not isinstance(optional, dict):
        return None

    # The official launcher names its optional package with the Node platform
    # suffix.  Iterate declared dependencies rather than inventing a package
    # name, while preferring the exact suffix when several are present.
    dependencies = [
        str(name) for name in optional
        if isinstance(name, str) and name.endswith(f"-{key}")
    ]
    for dependency in dependencies:
        dependency_root = package_root / "node_modules" / dependency
        dependency_manifest = _read_package_json(dependency_root / "package.json")
        if dependency_manifest is None or dependency_manifest.get("name") != dependency:
            continue
        declared_os = dependency_manifest.get("os")
        declared_cpu = dependency_manifest.get("cpu")
        node_os = "win32" if key.startswith("win32-") else key.split("-", 1)[0]
        node_cpu = key.split("-", 1)[1]
        if isinstance(declared_os, list) and declared_os and node_os not in declared_os:
            continue
        if isinstance(declared_cpu, list) and declared_cpu and node_cpu not in declared_cpu:
            continue
        executable = dependency_root / "bin" / ("wx.exe" if node_os == "win32" else "wx")
        if _is_native_wx_binary(executable):
            return executable
    return None


def _select_wx_binary() -> str:
    """Choose the native wx-cli binary, falling back to the original wrapper.

    Resolution is intentionally best-effort and happens once at module import.
    Any ambiguity, malformed npm metadata, unsupported platform, or missing
    executable returns the original command path so a package layout change
    cannot break the日报 pipeline.
    """
    explicit = os.environ.get("WX_CLI_BINARY")
    original = explicit or shutil.which("wx") or "/opt/homebrew/bin/wx"
    candidate = Path(original)
    if _is_native_wx_binary(candidate):
        return original
    native = _native_from_node_wrapper(candidate)
    return str(native) if native is not None else original


WX_BINARY = _select_wx_binary()

# Only download candidates worth vision's attention — matches vision.py's
# min_prefilter_score, so nothing downloaded here is thrown away downstream.
_MIN_DOWNLOAD_SCORE = 0.45

# `wx extract` is I/O-bound (daemon round-trip + decrypt); 4 workers keeps a
# 12-image batch under ~3 extract latencies without hammering the daemon.
_EXTRACT_MAX_WORKERS = 4


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


# The exporter summary is a blockquote line.  Anchor the match so a truncated
# response (or user message text that happens to say “导出 N 条消息”) cannot be
# mistaken for the authoritative count.
_COUNT_RE = re.compile(r"(?m)^>[ \t]*导出[ \t]+(\d+)[ \t]+条消息[ \t]*$")


def _message_count_summary(stdout: str) -> re.Match[str] | None:
    """Find the exporter-owned count before the first rendered message.

    Markdown message bodies are blockquoted by ``wx export``.  A user can
    therefore write a line such as ``导出 7 条消息`` and have it rendered as
    ``> 导出 7 条消息``—the same shape as the real preamble.  The preamble is
    the only place where the CLI emits the summary, so stop scanning at the
    first ``### YYYY-MM-DD HH:MM`` message heading before applying the anchored
    regex.  This keeps a body continuation from turning a truncated response
    into a trusted zero/positive result.
    """
    first_message = re.search(rf"(?m)^{_TS_HEADER}\s*$", stdout)
    preamble = stdout[: first_message.start()] if first_message else stdout
    return _COUNT_RE.search(preamble)

# ``wx export`` renders the daemon's freshness diagnostics on stderr and, for
# markdown/text output, also embeds them in stdout.  A zero-count response with
# one of these signals is not a trustworthy empty window: the daemon may have
# skipped a message shard while its decrypt/cache state was warming or being
# rebuilt.  Keep this deliberately narrow so an ordinary quiet window remains a
# valid zero-message export.
_INCOMPLETE_RE = re.compile(
    r"(?:"
    r"结果可能(?:过期|不完整)"
    r"|跳过了损坏的消息库"
    r"|skipped_shards"
    r"|possibly_stale(?:_unknown_shards)?"
    r"|\[!WARNING\]"
    r")",
    re.IGNORECASE,
)


def _incomplete_export_signal(stdout: str, stderr: str) -> str | None:
    """Return the first known incomplete-result marker from either stream."""
    combined = "\n".join(part for part in (stdout, stderr) if part)
    match = _INCOMPLETE_RE.search(combined)
    return match.group(0) if match else None


def _attachment_ids_by_local_id(group_name: str, since: str, until: str) -> dict[int, str]:
    """`wx attachments --json` local_id matches the local_id=NNN already parsed
    from export text — this maps that id to the opaque attachment_id `wx extract` needs."""
    cmd = [
        WX_BINARY, "attachments", group_name, "--kind", "image",
        "--since", since, "--until", until, "--json", "--limit", "10000",
    ]
    # Attachment lookup is an optional media enhancement.  A cold or unhealthy
    # daemon must not discard an otherwise valid text export merely because this
    # secondary request timed out or could not be started.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        log.warning("wx attachments lookup failed for %s: %s", group_name, exc)
        return {}
    if proc.returncode != 0:
        log.warning("wx attachments failed for %s: %s", group_name, proc.stderr or proc.stdout)
        return {}
    try:
        data = json.loads(proc.stdout)
        attachments = data.get("attachments", [])
        return {
            int(item["local_id"]): str(item["attachment_id"])
            for item in attachments
            if isinstance(item, dict) and "local_id" in item and "attachment_id" in item
        }
    except Exception as exc:
        log.warning("wx attachments returned invalid JSON for %s: %s", group_name, exc)
        return {}


def _decode_wxgf_in_place(path: Path) -> None:
    """`wx extract` often dumps WeChat's wxgf (HEVC) container under a .jpg name;
    PIL can't open it, so downstream media validation would drop the image.
    Best-effort transcode to real JPEG — also for pre-existing files from earlier
    runs. On failure the original file stays (status quo: dropped later)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if not is_wxgf(head):
            return
        if decode_wxgf(path, path):
            log.info("decoded wxgf container to JPEG: %s", path)
        else:
            log.warning("wxgf decode failed, keeping original file: %s", path)
    except Exception as e:
        log.warning("wxgf decode error for %s: %s", path, e)


def _extract_one_image(
    candidate: MediaCandidate, local_id: int, attachment_id: str | None, media_dir: Path,
) -> MediaCandidate | None:
    started = time.monotonic()
    out_path = media_dir / f"{local_id}.jpg"
    try:
        if not out_path.exists():
            if not attachment_id:
                return None  # A cached file disappeared after the preflight.
            proc = subprocess.run(
                [WX_BINARY, "extract", attachment_id, "-o", str(out_path)],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                log.warning(
                    "wx extract failed for local_id=%d: %s", local_id, proc.stderr or proc.stdout
                )
                return None
        _decode_wxgf_in_place(out_path)
    except Exception as e:
        log.warning("wx extract failed for local_id=%d: %s", local_id, e)
        return None
    log.debug("wx image local_id=%d ready in %.2fs", local_id, time.monotonic() - started)
    return replace(candidate, local_path=str(out_path))


def _download_wx_images(
    candidates: list[MediaCandidate], *, group_name: str, since: str, until: str, media_dir: Path,
    min_score: float = _MIN_DOWNLOAD_SCORE,
) -> list[MediaCandidate]:
    """Extract score-qualifying image candidates via `wx extract`, in place.

    Only candidates already worth vision's attention are downloaded — the score comes
    from message-text keywords alone (media.py), so this needs no image data up front.
    Extracts run on a small thread pool; per-item failures are logged and skipped,
    they never abort the export, and result order always matches candidate order.
    """
    qualifies = {
        idx for idx, c in enumerate(candidates)
        if c.media_type == "图片" and c.score >= min_score and c.raw_ref
    }
    if not qualifies:
        return candidates
    local_ids = {idx: int(candidates[idx].raw_ref.split("=", 1)[1]) for idx in qualifies}
    cached = {idx for idx, local_id in local_ids.items()
              if (media_dir / f"{local_id}.jpg").is_file()}
    # Existing images do not depend on the CLI/attachment database still being
    # available. Re-run wxgf decoding for them, but only query missing media.
    by_local_id = (
        _attachment_ids_by_local_id(group_name, since, until)
        if qualifies - cached else {}
    )
    if not cached and not by_local_id:
        return candidates
    if by_local_id:
        media_dir.mkdir(parents=True, exist_ok=True)
    updated: dict[int, MediaCandidate] = {}
    with ThreadPoolExecutor(max_workers=_EXTRACT_MAX_WORKERS) as pool:
        futures = {}
        for idx in sorted(qualifies):
            c = candidates[idx]
            local_id = local_ids[idx]
            attachment_id = by_local_id.get(local_id)
            if not attachment_id and idx not in cached:
                continue
            futures[pool.submit(_extract_one_image, c, local_id, attachment_id, media_dir)] = idx
        for future, idx in futures.items():
            result = future.result()
            if result is not None:
                updated[idx] = result
    return [updated.get(i, c) for i, c in enumerate(candidates)]


def _export_group_impl(
    group_name: str,
    since: str,
    until: str,
    out_path: Path,
    limit: int = 10000,
    min_download_score: float = _MIN_DOWNLOAD_SCORE,
    *,
    download_images: bool = True,
) -> ExportResult:
    """Run `wx export <group>` capturing markdown from stdout, clean, write once.

    Raises RuntimeError on non-zero exit.
    """
    cmd = [
        # ``--with-meta`` is a global clap flag.  Keeping it after the
        # subcommand preserves compatibility with callers/mocks that inspect
        # ``cmd[1]`` while clap still forwards the flag to the daemon request.
        WX_BINARY, "export", group_name,
        "--since", since, "--until", until,
        "--limit", str(limit), "--format", "markdown", "--with-meta",
    ]
    # The wx daemon reports ready (contacts loaded) ~1.5s before it finishes decrypting the
    # message DB. On a cold daemon — the usual case for the unattended launchd run after the
    # Mac has been idle — the first `wx export` races that decryption and returns a non-zero
    # "找不到消息记录" exit or an empty result. Retry a few times so the daemon can finish
    # warming before an empty result is taken at face value. A genuinely quiet day still exits
    # the loop after the retries and returns 0 messages without raising.
    proc = None
    m = None
    incomplete_signals: list[str] = []
    attempt_failures: list[str] = []
    last_timeout = None
    zero_tainted = False
    accepted_clean_positive = False
    for attempt in range(4):
        proc = None
        m = None
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
            zero_tainted = True
            attempt_failures.append("timeout")
            if attempt < 3:
                time.sleep(2.0)
            continue
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        m = _message_count_summary(stdout)
        if proc.returncode != 0:
            zero_tainted = True
            detail = (stderr or stdout).strip()
            attempt_failures.append(
                f"exit {proc.returncode}: {detail[:200]}"
                if detail
                else f"exit {proc.returncode}"
            )
        elif m is None:
            # A malformed success cannot be used as evidence for a clean zero.
            zero_tainted = True
            attempt_failures.append("missing message-count summary")
        incomplete_signal = _incomplete_export_signal(stdout, stderr)
        if incomplete_signal:
            incomplete_signals.append(incomplete_signal)
            zero_tainted = True
            attempt_failures.append(incomplete_signal)
        if proc.returncode == 0 and m and int(m.group(1)) > 0 and not incomplete_signal:
            accepted_clean_positive = True
            break
        if attempt < 3:
            time.sleep(2.0)
    if proc is None:
        detail = attempt_failures[-1] if attempt_failures else str(last_timeout)
        raise RuntimeError(f"wx export timed out for {group_name}: {detail}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or attempt_failures[-1]
        raise RuntimeError(f"wx export failed: {detail}")
    if m is None:
        # A successful process without the exporter summary is not evidence of
        # an empty window.  Treating a truncated/malformed response as zero
        # would have the same destructive effect as trusting an incomplete
        # daemon result: the prior archive could be replaced by an empty file.
        raise RuntimeError(
            f"wx export returned an unparseable response for {group_name}: "
            "missing message-count summary"
        )
    raw = proc.stdout
    count = int(m.group(1))
    if zero_tainted and not accepted_clean_positive:
        # Do not parse media or touch ``out_path`` for an untrusted response.
        # This applies to both zero-count and partially positive responses:
        # a positive count with skipped shards is still incomplete and could
        # replace the only durable copy of messages from those shards.
        detail = incomplete_signals[-1] if incomplete_signals else attempt_failures[-1]
        raise RuntimeError(
            f"wx export returned an incomplete result for {group_name} "
            "(zero-count mismatch or partial positive): "
            f"result may be incomplete ({detail})"
        )
    media_candidates = extract_wx_media_candidates(raw, group_name=group_name)
    if download_images:
        media_dir = out_path.parent / "wx_media" / safe_filename(group_name)
        media_candidates = _download_wx_images(
            media_candidates, group_name=group_name, since=since, until=until, media_dir=media_dir,
            min_score=min_download_score,
        )
    cleaned = clean_wx_markdown(raw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(cleaned, encoding="utf-8")
    return ExportResult(
        group_name=group_name, out_path=out_path,
        message_count=count, content=cleaned,
        media_candidates=media_candidates,
    )


def export_group(
    group_name: str, since: str, until: str, out_path: Path,
    limit: int = 10000, min_download_score: float = _MIN_DOWNLOAD_SCORE,
    *, download_images: bool = True,
) -> ExportResult:
    """Export and retain a best-effort acquisition receipt beside the archive."""
    from datetime import datetime, timezone
    from chat_daily_tg.fetch_health import record_fetch
    started=datetime.now(timezone.utc).isoformat()
    journal=Path(out_path).parent/'fetch_health.jsonl'
    try:
        result=_export_group_impl(group_name,since,until,out_path,limit,min_download_score,
                                  download_images=download_images)
    except Exception as exc:
        record_fetch(journal,producer='wechat',source_ref=group_name,started_at=started,
                     status='failed',error_type=type(exc).__name__)
        raise
    status='no_update' if result.message_count==0 else ('success' if result.content.strip() else 'parsed_empty')
    record_fetch(journal,producer='wechat',source_ref=group_name,started_at=started,
                 status=status,count=result.message_count)
    return result
