from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from chat_daily_tg.knowledge_release import (
    CANARY_PENDING,
    OPEN,
    SCHEMA,
    prepare_release,
    promote_release,
    read_release_state,
    validate_release_state,
)


EVALUATION_HASH = "a" * 64
SHADOW_HASH = "b" * 64
CANDIDATE_ARTIFACT_HASH = "c" * 64
BASELINE_ARTIFACT_HASH = "d" * 64
PREPARED = "2026-08-25T10:00:00+00:00"
PROMOTED = "2026-08-25T10:05:00+00:00"
NEXT_PREPARED = "2026-08-25T10:10:00+00:00"


def _prepare(path: Path, **overrides):
    values = {
        "candidate_generation": "candidate-v2",
        "baseline_generation": "baseline-v1",
        "candidate_artifact_sha256": CANDIDATE_ARTIFACT_HASH,
        "baseline_artifact_sha256": BASELINE_ARTIFACT_HASH,
        "evaluation_hash": EVALUATION_HASH,
        "shadow_journal_hash": SHADOW_HASH,
        "prepared_at": PREPARED,
    }
    values.update(overrides)
    return prepare_release(path, **values)


def test_missing_release_state_is_not_open(tmp_path: Path) -> None:
    assert read_release_state(tmp_path / "release.json") is None


def test_prepare_writes_exact_durable_pending_schema(tmp_path: Path) -> None:
    path = tmp_path / "release.json"

    state = _prepare(path)

    assert state == {
        "schema": SCHEMA,
        "status": CANARY_PENDING,
        "candidate_generation": "candidate-v2",
        "baseline_generation": "baseline-v1",
        "candidate_artifact_sha256": CANDIDATE_ARTIFACT_HASH,
        "baseline_artifact_sha256": BASELINE_ARTIFACT_HASH,
        "evaluation_hash": EVALUATION_HASH,
        "shadow_journal_hash": SHADOW_HASH,
        "prepared_at": PREPARED,
    }
    assert read_release_state(path) == state
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".release.json.tmp.*"))


def test_release_state_read_and_write_reject_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "release.json"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        read_release_state(path)
    with pytest.raises(ValueError, match="regular non-symlink"):
        _prepare(path)
    assert target.read_text(encoding="utf-8") == "{}"


def test_release_state_rejects_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _prepare(real / "release.json")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink directory"):
        read_release_state(linked / "release.json")
    with pytest.raises(ValueError, match="non-symlink directory"):
        _prepare(linked / "release.json")
    assert read_release_state(real / "release.json")["status"] == CANARY_PENDING


def test_release_state_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="regular file"):
        read_release_state(path)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b"[]\n",
        (
            b'{"schema":"chatdaily-knowledge-release.v1",'
            b'"schema":"chatdaily-knowledge-release.v1"}\n'
        ),
    ],
)
def test_corrupt_release_state_fails_closed(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "release.json"
    path.write_bytes(payload)

    with pytest.raises(ValueError):
        read_release_state(path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        _prepare(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate_generation", "../candidate", "candidate_generation"),
        ("baseline_generation", "/absolute", "baseline_generation"),
        ("candidate_artifact_sha256", "A" * 64, "lowercase SHA-256"),
        ("baseline_artifact_sha256", "short", "lowercase SHA-256"),
        ("evaluation_hash", "A" * 64, "lowercase SHA-256"),
        ("shadow_journal_hash", "short", "lowercase SHA-256"),
        ("prepared_at", "2026-08-25T10:00:00", "canonical UTC"),
        ("prepared_at", "", "canonical UTC"),
        ("prepared_at", "2026-08-25T18:00:00+08:00", "canonical UTC"),
        ("prepared_at", "2026-08-25T10:00:00Z", "canonical UTC"),
    ],
)
def test_prepare_strictly_validates_ids_hashes_and_timestamp(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _prepare(tmp_path / "release.json", **{field: value})


def test_candidate_must_differ_from_baseline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must differ"):
        _prepare(
            tmp_path / "release.json",
            candidate_generation="same",
            baseline_generation="same",
        )


def test_state_validation_rejects_unknown_or_missing_fields() -> None:
    pending = {
        "schema": SCHEMA,
        "status": CANARY_PENDING,
        "candidate_generation": "candidate-v2",
        "baseline_generation": "baseline-v1",
        "candidate_artifact_sha256": CANDIDATE_ARTIFACT_HASH,
        "baseline_artifact_sha256": BASELINE_ARTIFACT_HASH,
        "evaluation_hash": EVALUATION_HASH,
        "shadow_journal_hash": SHADOW_HASH,
        "prepared_at": PREPARED,
    }
    with pytest.raises(ValueError, match="extra"):
        validate_release_state({**pending, "unexpected": True})
    missing = dict(pending)
    del missing["evaluation_hash"]
    with pytest.raises(ValueError, match="missing"):
        validate_release_state(missing)
    with pytest.raises(ValueError, match="status"):
        validate_release_state({**pending, "status": "ready"})


def test_promote_requires_matching_pending_generation(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    pending = _prepare(path)

    with pytest.raises(ValueError, match="does not match"):
        promote_release(path, generation_id="other-v3", promoted_at=PROMOTED)

    assert read_release_state(path) == pending


def test_promote_rejects_missing_state_and_invalid_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    with pytest.raises(ValueError, match="missing"):
        promote_release(path, generation_id="candidate-v2", promoted_at=PROMOTED)
    _prepare(path)
    with pytest.raises(ValueError, match="canonical UTC"):
        promote_release(
            path,
            generation_id="candidate-v2",
            promoted_at="2026-08-25T10:05:00Z",
        )
    with pytest.raises(ValueError, match="cannot precede"):
        promote_release(
            path,
            generation_id="candidate-v2",
            promoted_at="2026-08-25T09:59:59+00:00",
        )


def test_repeated_pending_is_idempotent_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    first = _prepare(path)
    first_bytes = path.read_bytes()
    first_inode = path.stat().st_ino

    repeated = _prepare(path, prepared_at="2026-08-25T10:01:00+00:00")

    assert repeated == first
    assert path.read_bytes() == first_bytes
    assert path.stat().st_ino == first_inode


def test_same_candidate_with_different_evidence_cannot_rewrite_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.json"
    pending = _prepare(path)

    with pytest.raises(ValueError, match="unresolved canary_pending"):
        _prepare(path, evaluation_hash="c" * 64)

    assert read_release_state(path) == pending


def test_promote_is_atomic_and_idempotent_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    _prepare(path)

    opened = promote_release(
        path, generation_id="candidate-v2", promoted_at=PROMOTED
    )
    opened_bytes = path.read_bytes()
    opened_inode = path.stat().st_ino
    repeated = promote_release(
        path,
        generation_id="candidate-v2",
        promoted_at="2026-08-25T10:06:00+00:00",
    )

    assert opened["status"] == OPEN
    assert opened["promoted_at"] == PROMOTED
    assert repeated == opened
    assert path.read_bytes() == opened_bytes
    assert path.stat().st_ino == opened_inode

    with pytest.raises(ValueError, match="cannot precede"):
        promote_release(
            path,
            generation_id="candidate-v2",
            promoted_at="2026-08-25T09:59:59+00:00",
        )
    assert path.read_bytes() == opened_bytes


def test_prepare_retry_cannot_demote_matching_open_release(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    _prepare(path)
    opened = promote_release(
        path, generation_id="candidate-v2", promoted_at=PROMOTED
    )
    opened_bytes = path.read_bytes()

    repeated = _prepare(path, prepared_at=NEXT_PREPARED)

    assert repeated == opened
    assert repeated["status"] == OPEN
    assert path.read_bytes() == opened_bytes


def test_atomic_write_fsyncs_file_and_directory_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync_calls: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    def recording_replace(source, destination) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    path = tmp_path / "release.json"
    _prepare(path)

    assert len(fsync_calls) >= 2
    assert len(replacements) == 1
    assert replacements[0][1] == path
    assert read_release_state(path)["status"] == CANARY_PENDING


def test_different_release_cannot_overwrite_unresolved_pending(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    pending = _prepare(path)

    with pytest.raises(ValueError, match="unresolved canary_pending"):
        _prepare(
            path,
            candidate_generation="candidate-v3",
            baseline_generation="candidate-v2",
            prepared_at=NEXT_PREPARED,
        )

    assert read_release_state(path) == pending


def test_new_release_must_chain_from_open_generation_and_be_newer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.json"
    _prepare(path)
    opened = promote_release(
        path, generation_id="candidate-v2", promoted_at=PROMOTED
    )

    with pytest.raises(ValueError, match="baseline must equal"):
        _prepare(
            path,
            candidate_generation="candidate-v3",
            baseline_generation="unrelated-v1",
            prepared_at=NEXT_PREPARED,
        )
    with pytest.raises(ValueError, match="must follow"):
        _prepare(
            path,
            candidate_generation="candidate-v3",
            baseline_generation="candidate-v2",
            prepared_at=PROMOTED,
        )
    assert read_release_state(path) == opened

    next_pending = _prepare(
        path,
        candidate_generation="candidate-v3",
        baseline_generation="candidate-v2",
        evaluation_hash="c" * 64,
        shadow_journal_hash="d" * 64,
        prepared_at=NEXT_PREPARED,
    )
    assert next_pending["status"] == CANARY_PENDING
    assert next_pending["candidate_generation"] == "candidate-v3"
    assert read_release_state(path) == next_pending


def test_release_transitions_never_touch_pointers_or_kill_switch(
    tmp_path: Path,
) -> None:
    current = tmp_path / "CURRENT"
    previous = tmp_path / "PREVIOUS"
    kill_switch = tmp_path / "RETRIEVAL_DISABLED"
    current.write_text("baseline-v1\n", encoding="utf-8")
    previous.write_text("older-v0\n", encoding="utf-8")
    kill_switch.write_text("disabled\n", encoding="utf-8")
    before = {
        current: current.read_bytes(),
        previous: previous.read_bytes(),
        kill_switch: kill_switch.read_bytes(),
    }

    path = tmp_path / "release.json"
    _prepare(path)
    promote_release(path, generation_id="candidate-v2", promoted_at=PROMOTED)

    assert {item: item.read_bytes() for item in before} == before
