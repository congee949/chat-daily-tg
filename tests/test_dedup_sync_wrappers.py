"""Hermetic wrapper checks: copied scripts, fake Python, no SSH or Telegram calls."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _environment(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    calls = tmp_path / "calls.log"
    env = os.environ.copy()
    env.update(
        {
            "CHAT_DAILY_DATA_DIR": str(tmp_path / "data"),
            "CHAT_DAILY_CHANNELS_LOCK_DIR": str(tmp_path / "channels.lock"),
            "CHAT_DAILY_NO_JITTER": "1",
            "TEST_CALLS": str(calls),
            "TEST_MIRROR_RC": "0",
            "TEST_CHANNELS_RC": "0",
        }
    )
    return project, env, calls


def _copy_wrapper(project: Path, name: str) -> Path:
    path = project / "scripts" / name
    shutil.copyfile(ROOT / "scripts" / name, path)
    return path


def _run(wrapper: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(wrapper)], env=env, capture_output=True, text=True, timeout=10
    )


def _channels_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project, env, calls = _environment(tmp_path)
    wrapper = _copy_wrapper(project, "run_channels_guarded.sh")
    # Replace all shared guard actions in the temporary checkout, including the
    # notification path and caffeinate, before executing the copied wrapper.
    (project / "scripts" / "guard_common.sh").write_text(
        """guard_setup_env() { :; }
guard_notify() { printf 'notify %s\n' "$*" >> "$TEST_CALLS"; }
guard_heartbeat() { printf 'heartbeat %s\n' "$*" >> "$TEST_CALLS"; }
guard_jitter_sleep() { printf 'jitter\n' >> "$TEST_CALLS"; }
""",
        encoding="utf-8",
    )
    fake_caffeinate = _executable(
        tmp_path / "caffeinate",
        '#!/bin/bash\nshift\nexec "$@"\n',
    )
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8").replace(
            "/usr/bin/caffeinate", str(fake_caffeinate)
        ),
        encoding="utf-8",
    )
    python_stub = _executable(
        tmp_path / "python-stub",
        """#!/bin/bash
lock=no
if [ "$(cat "$CHAT_DAILY_CHANNELS_LOCK_DIR/pid" 2>/dev/null)" = "$PPID" ]; then lock=yes; fi
case "$1" in
  */sync_xmonitor_sent_content.py)
    shift
    printf 'mirror lock=%s %s\n' "$lock" "$*" >> "$TEST_CALLS"
    exit "$TEST_MIRROR_RC"
    ;;
  */run_daily.py)
    printf 'channels lock=%s\n' "$lock" >> "$TEST_CALLS"
    exit "$TEST_CHANNELS_RC"
    ;;
  *) exit 99 ;;
esac
""",
    )
    env["CHAT_DAILY_PY"] = str(python_stub)
    return wrapper, env, calls


@pytest.mark.parametrize("mirror_rc", [0, 1, 124])
def test_channels_refreshes_mirror_under_lock_and_failure_is_fail_open(tmp_path, mirror_rc):
    wrapper, env, calls = _channels_fixture(tmp_path)
    env["TEST_MIRROR_RC"] = str(mirror_rc)

    result = _run(wrapper, env)

    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    mirror = (
        f"mirror lock=yes --destination {env['CHAT_DAILY_DATA_DIR']}"
        "/state/xmonitor_sent_snapshot.json"
    )
    assert lines == [mirror, "jitter", "channels lock=yes", "heartbeat channels 0"]
    assert not Path(env["CHAT_DAILY_CHANNELS_LOCK_DIR"]).exists()
    logs = list((Path(env["CHAT_DAILY_DATA_DIR"]) / "logs").glob("guard-channels-*.log"))
    assert len(logs) == 1
    assert f"xmonitor-caption-mirror exit={mirror_rc}" in logs[0].read_text()


def test_channels_lock_skip_does_not_refresh_mirror(tmp_path):
    wrapper, env, calls = _channels_fixture(tmp_path)
    lock = Path(env["CHAT_DAILY_CHANNELS_LOCK_DIR"])
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))

    result = _run(wrapper, env)

    assert result.returncode == 0
    assert calls.read_text().splitlines() == ["heartbeat channels 0"]
    assert (lock / "pid").read_text() == str(os.getpid())


def test_channels_failure_keeps_forwarder_exit_code_after_failed_mirror(tmp_path):
    wrapper, env, calls = _channels_fixture(tmp_path)
    env.update(TEST_MIRROR_RC="1", TEST_CHANNELS_RC="7")

    result = _run(wrapper, env)

    assert result.returncode == 7
    lines = calls.read_text().splitlines()
    assert "channels lock=yes" in lines
    assert any(line.startswith("notify ") for line in lines)
    assert lines[-1] == "heartbeat channels 7"
    assert not Path(env["CHAT_DAILY_CHANNELS_LOCK_DIR"]).exists()


@pytest.mark.parametrize("content_rc", [0, 3])
def test_minute_ledger_sync_pushes_content_without_pulling_media(tmp_path, content_rc):
    project, env, calls = _environment(tmp_path)
    wrapper = _copy_wrapper(project, "run_ledger_sync_guarded.sh")
    for name, stage, rc in (
        ("sync_media_ledger.sh", "unexpected-media", 99),
        ("sync_sent_content_ledger.sh", "content", content_rc),
    ):
        _executable(
            project / "scripts" / name,
            f'#!/bin/bash\nprintf "{stage}\\n" >> "$TEST_CALLS"\nexit {rc}\n',
        )
    (project / ".venv" / "bin").mkdir(parents=True)
    _executable(
        project / ".venv" / "bin" / "python",
        '#!/bin/bash\nprintf "unexpected-python\\n" >> "$TEST_CALLS"\nexit 99\n',
    )

    result = _run(wrapper, env)

    assert result.returncode == content_rc
    assert calls.read_text().splitlines() == ["content"]
    logs = list((Path(env["CHAT_DAILY_DATA_DIR"]) / "logs").glob("guard-ledger-sync-*.log"))
    assert len(logs) == 1
    assert f"end ledger-sync exit={content_rc}" in logs[0].read_text()


def test_sent_content_sync_script_is_executable():
    assert os.access(ROOT / "scripts" / "sync_sent_content_ledger.sh", os.X_OK)
