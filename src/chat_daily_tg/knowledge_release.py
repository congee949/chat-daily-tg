"""Durable release state for the KnowledgeIndex query canary.

This module owns only the release-state file supplied by the caller and a
sibling advisory lock.  It deliberately has no dependency on generation
pointers or the retrieval kill switch: publishing a release state must never
switch ``CURRENT``/``PREVIOUS`` or silently re-enable retrieval.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Iterator


SCHEMA = "chatdaily-knowledge-release.v1"
CANARY_PENDING = "canary_pending"
OPEN = "open"
RELEASE_STATE_FILENAME = ".release-state.json"

_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STATE_BYTES = 64 * 1024
_BASE_KEYS = frozenset(
    {
        "schema",
        "status",
        "candidate_generation",
        "baseline_generation",
        "candidate_artifact_sha256",
        "baseline_artifact_sha256",
        "evaluation_hash",
        "shadow_journal_hash",
        "prepared_at",
    }
)
_OPEN_KEYS = _BASE_KEYS | {"promoted_at"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_generation_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or (
        not value
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
        or _GENERATION_ID.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _validate_hash(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _validate_timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise ValueError(f"{field} must be a canonical UTC timestamp")
    return value, parsed


def validate_release_state(value: Any) -> dict[str, Any]:
    """Return a validated copy of one exact v1 release-state object."""

    if not isinstance(value, dict):
        raise ValueError("knowledge release state must be a JSON object")
    state = dict(value)
    if state.get("schema") != SCHEMA:
        raise ValueError(f"unknown knowledge release schema: {state.get('schema')!r}")
    status = state.get("status")
    if status not in {CANARY_PENDING, OPEN}:
        raise ValueError(f"invalid knowledge release status: {status!r}")
    expected_keys = _OPEN_KEYS if status == OPEN else _BASE_KEYS
    if frozenset(state) != expected_keys:
        missing = sorted(expected_keys - frozenset(state))
        extra = sorted(frozenset(state) - expected_keys)
        raise ValueError(
            f"invalid knowledge release fields: missing={missing}, extra={extra}"
        )

    candidate = _validate_generation_id(
        state["candidate_generation"], field="candidate_generation"
    )
    baseline = _validate_generation_id(
        state["baseline_generation"], field="baseline_generation"
    )
    if candidate == baseline:
        raise ValueError("candidate_generation must differ from baseline_generation")
    state["candidate_generation"] = candidate
    state["baseline_generation"] = baseline
    state["candidate_artifact_sha256"] = _validate_hash(
        state["candidate_artifact_sha256"], field="candidate_artifact_sha256"
    )
    state["baseline_artifact_sha256"] = _validate_hash(
        state["baseline_artifact_sha256"], field="baseline_artifact_sha256"
    )
    state["evaluation_hash"] = _validate_hash(
        state["evaluation_hash"], field="evaluation_hash"
    )
    state["shadow_journal_hash"] = _validate_hash(
        state["shadow_journal_hash"], field="shadow_journal_hash"
    )
    state["prepared_at"], prepared = _validate_timestamp(
        state["prepared_at"], field="prepared_at"
    )
    if status == OPEN:
        state["promoted_at"], promoted = _validate_timestamp(
            state["promoted_at"], field="promoted_at"
        )
        if promoted < prepared:
            raise ValueError("promoted_at cannot precede prepared_at")
    return state


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate knowledge release field: {key}")
        value[key] = item
    return value


def _secure_read_json(path: Path) -> dict[str, Any] | None:
    _assert_safe_parent(path)
    # O_NONBLOCK prevents an attacker-controlled FIFO from hanging before the
    # fstat regular-file check can reject it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EFTYPE if hasattr(errno, "EFTYPE") else -1}:
            raise ValueError(
                f"knowledge release state must be a regular non-symlink file: {path}"
            ) from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError(
                f"knowledge release state must be a user-owned regular file: {path}"
            )
        if metadata.st_size > _MAX_STATE_BYTES:
            raise ValueError("knowledge release state exceeds the size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_STATE_BYTES + 1)
        if len(payload) > _MAX_STATE_BYTES:
            raise ValueError("knowledge release state exceeds the size limit")
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed knowledge release state: {path}") from exc
    return validate_release_state(raw)


def read_release_state(path: Path) -> dict[str, Any] | None:
    """Securely read and strictly validate a release state, or return ``None``."""

    return _secure_read_json(Path(path).expanduser())


def _assert_safe_parent(path: Path) -> None:
    try:
        metadata = os.lstat(path.parent)
    except FileNotFoundError as exc:
        raise ValueError(f"knowledge release state parent is missing: {path.parent}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError(
            "knowledge release state parent must be a user-owned "
            f"non-symlink directory: {path.parent}"
        )


def _assert_safe_existing(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError(f"refusing unsafe knowledge release state path: {path}")


@contextmanager
def _release_lock(path: Path) -> Iterator[None]:
    _assert_safe_parent(path)
    lock_path = path.parent / f".{path.name}.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"cannot open secure knowledge release lock: {lock_path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError(
                f"knowledge release lock is not a user-owned regular file: {lock_path}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    value = validate_release_state(value)
    _assert_safe_parent(path)
    _assert_safe_existing(path)
    temporary = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        _assert_safe_existing(path)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            metadata = os.lstat(temporary)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                os.unlink(temporary)


def _same_release_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left[key] == right[key]
        for key in (
            "candidate_generation",
            "baseline_generation",
            "candidate_artifact_sha256",
            "baseline_artifact_sha256",
            "evaluation_hash",
            "shadow_journal_hash",
        )
    )


def prepare_release(
    path: Path,
    *,
    candidate_generation: str,
    baseline_generation: str,
    candidate_artifact_sha256: str,
    baseline_artifact_sha256: str,
    evaluation_hash: str,
    shadow_journal_hash: str,
    prepared_at: str | None = None,
) -> dict[str, Any]:
    """Durably publish ``canary_pending`` without clobbering an active canary.

    A later generation may replace an ``open`` state only when it names that
    open generation as its baseline and carries a strictly newer timestamp.
    """

    path = Path(path).expanduser()
    pending = validate_release_state(
        {
            "schema": SCHEMA,
            "status": CANARY_PENDING,
            "candidate_generation": candidate_generation,
            "baseline_generation": baseline_generation,
            "candidate_artifact_sha256": candidate_artifact_sha256,
            "baseline_artifact_sha256": baseline_artifact_sha256,
            "evaluation_hash": evaluation_hash,
            "shadow_journal_hash": shadow_journal_hash,
            "prepared_at": prepared_at if prepared_at is not None else utc_now(),
        }
    )
    with _release_lock(path):
        current = _secure_read_json(path)
        if current is None:
            _atomic_write(path, pending)
            return pending
        if _same_release_evidence(current, pending):
            # Never demote an already-open release, and never rewrite a stable
            # pending record merely because a retry produced a new wall clock.
            return current
        if current["status"] == CANARY_PENDING:
            raise ValueError("cannot replace an unresolved canary_pending release")
        if pending["baseline_generation"] != current["candidate_generation"]:
            raise ValueError("new release baseline must equal the open generation")
        _, new_prepared = _validate_timestamp(pending["prepared_at"], field="prepared_at")
        _, old_promoted = _validate_timestamp(current["promoted_at"], field="promoted_at")
        if new_prepared <= old_promoted:
            raise ValueError("new release prepared_at must follow the open release")
        _atomic_write(path, pending)
        return pending


def promote_release(
    path: Path,
    *,
    generation_id: str,
    promoted_at: str | None = None,
) -> dict[str, Any]:
    """Atomically promote only the matching pending generation to ``open``."""

    path = Path(path).expanduser()
    generation_id = _validate_generation_id(generation_id, field="generation_id")
    supplied_promoted_at = promoted_at
    requested_promotion: datetime | None = None
    if supplied_promoted_at is not None:
        _, requested_promotion = _validate_timestamp(
            supplied_promoted_at, field="promoted_at"
        )
    with _release_lock(path):
        current = _secure_read_json(path)
        if current is None:
            raise ValueError("cannot promote a missing knowledge release state")
        if current["candidate_generation"] != generation_id:
            raise ValueError("pending release generation does not match promotion target")
        if requested_promotion is not None:
            _, prepared = _validate_timestamp(
                current["prepared_at"], field="prepared_at"
            )
            if requested_promotion < prepared:
                raise ValueError("promoted_at cannot precede prepared_at")
        if current["status"] == OPEN:
            return current
        promoted = dict(current)
        promoted["status"] = OPEN
        promoted["promoted_at"] = supplied_promoted_at or utc_now()
        promoted = validate_release_state(promoted)
        _atomic_write(path, promoted)
        return promoted
