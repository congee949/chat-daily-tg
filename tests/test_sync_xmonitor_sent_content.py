"""Caption-sync CLI tests without a live SSH connection."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from chat_daily_tg.sent_content_mirror import read_snapshot, write_snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_script():
    spec = importlib.util.spec_from_file_location(
        "sync_xmonitor_sent_content", ROOT / "scripts" / "sync_xmonitor_sent_content.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ledger_bytes():
    content = "模型发布，价格与可用范围已确认。"
    row = {
        "schema": "sent-content.v1",
        "delivery_state": "confirmed",
        "producer": "x_monitor",
        "chat_id": -100123,
        "message_id": 7,
        "thread_id": 19,
        "content": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    return (json.dumps(row) + "\n").encode()


def test_ssh_uses_sync_options_and_quoted_remote_path(
    tmp_path, sync_script, ledger_bytes, monkeypatch, capsys
):
    destination = tmp_path / "snapshot.json"
    remote = "/root/x monitor/state/caption's ledger.jsonl"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=ledger_bytes)

    monkeypatch.setattr(sync_script.subprocess, "run", fake_run)

    assert sync_script.main(
        ["--host", "fixture-host", "--remote", remote, "--destination", str(destination)]
    ) == 0
    assert calls == [(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new", "fixture-host",
            "cat -- " + shlex.quote(remote),
        ],
        {"check": True, "capture_output": True, "timeout": 30},
    )]
    assert read_snapshot(destination)["source"] == "fixture-host:" + remote
    assert json.loads(capsys.readouterr().out)["status"] == "synced"


@pytest.mark.parametrize("failure", ["ssh", "invalid-ledger"])
def test_failed_refresh_keeps_last_good_snapshot(
    tmp_path, sync_script, ledger_bytes, monkeypatch, capsys, failure
):
    destination = tmp_path / "snapshot.json"
    write_snapshot(ledger_bytes, destination, source="existing")
    before = destination.read_bytes()

    def fake_run(*args, **kwargs):
        if failure == "ssh":
            raise subprocess.CalledProcessError(255, ["ssh"])
        return SimpleNamespace(stdout=b"invalid jsonl")

    monkeypatch.setattr(sync_script.subprocess, "run", fake_run)

    assert sync_script.main(["--destination", str(destination)]) == 1
    assert destination.read_bytes() == before
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_source_file_and_check_are_offline_and_check_does_not_write(
    tmp_path, sync_script, ledger_bytes, monkeypatch, capsys
):
    source = tmp_path / "ledger.jsonl"
    source.write_bytes(ledger_bytes)
    destination = tmp_path / "snapshot.json"

    def forbidden_run(*args, **kwargs):
        raise AssertionError("offline operation attempted SSH")

    monkeypatch.setattr(sync_script.subprocess, "run", forbidden_run)
    assert sync_script.main(
        ["--source-file", str(source), "--destination", str(destination)]
    ) == 0
    capsys.readouterr()
    before = (destination.read_bytes(), destination.stat().st_mtime_ns)

    assert sync_script.main(["--check", "--destination", str(destination)]) == 0
    assert (destination.read_bytes(), destination.stat().st_mtime_ns) == before
    assert json.loads(capsys.readouterr().out)["status"] == "fresh"
