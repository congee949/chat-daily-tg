from types import SimpleNamespace
import plistlib

import pytest

from chat_daily_tg.config import DedupTopic
from chat_daily_tg import qwen_runtime


def cfg(enabled=True):
    topic = DedupTopic(enabled=True, qwen_runtime_on_demand=enabled)
    return SimpleNamespace(
        sources=SimpleNamespace(telegram=SimpleNamespace(dedup=SimpleNamespace(topic=topic))),
        models=SimpleNamespace(embedding=SimpleNamespace(enabled=True, provider="openai",
            endpoint="http://127.0.0.1:8790/v1", api_key_env="TEST_KEY")),
    )


def test_disabled_and_no_push_do_not_start_models(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("runtime management must be disabled")
    monkeypatch.setattr(qwen_runtime, "_health", forbidden)
    for config, no_push in ((cfg(False), False), (cfg(), True)):
        with qwen_runtime.channel_runtime(config, no_push=no_push):
            pass


def test_existing_runtime_is_not_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(qwen_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(qwen_runtime, "_health", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(qwen_runtime, "_launchctl", lambda *a: calls.append(a))
    with qwen_runtime.channel_runtime(cfg()):
        pass
    assert calls == []


@pytest.mark.parametrize("night,raise_inside", [(False, False), (False, True), (True, False)])
def test_owned_runtime_released_after_run_or_failure(monkeypatch, tmp_path, night, raise_inside):
    home = tmp_path/"home"
    plist = home/"Library/LaunchAgents"/f"{qwen_runtime.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": qwen_runtime.LABEL}))
    monkeypatch.setattr(qwen_runtime.Path, "home", lambda: home)
    monkeypatch.setattr(qwen_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    health = iter([False, True])
    monkeypatch.setattr(qwen_runtime, "_health", lambda *a, **k: next(health))
    monkeypatch.setattr(qwen_runtime, "_night_window", lambda: night)
    calls = []
    def launchctl(*args):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[0] == "print" else 0)
    monkeypatch.setattr(qwen_runtime, "_launchctl", launchctl)
    try:
        with qwen_runtime.channel_runtime(cfg()):
            if raise_inside:
                raise RuntimeError("delivery failed")
    except RuntimeError:
        assert raise_inside
    assert any(args[0] == "bootstrap" for args in calls)
    assert any(args[0] == "bootout" for args in calls) is (not night)


def test_startup_failure_does_not_block_channel_work(monkeypatch, tmp_path):
    monkeypatch.setattr(qwen_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(qwen_runtime, "_health", lambda *a, **k: False)
    def fail(*args):
        raise RuntimeError("launchd unavailable")
    monkeypatch.setattr(qwen_runtime, "_launchctl", fail)
    reached = False
    with qwen_runtime.channel_runtime(cfg()):
        reached = True
    assert reached


def test_dying_runtime_is_bootstrapped_after_unload(monkeypatch, tmp_path):
    home = tmp_path / "home"
    plist = home / "Library/LaunchAgents" / f"{qwen_runtime.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": qwen_runtime.LABEL}))
    monkeypatch.setattr(qwen_runtime.Path, "home", lambda: home)
    monkeypatch.setattr(qwen_runtime.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(qwen_runtime.time, "sleep", lambda _: None)
    monkeypatch.setattr(qwen_runtime, "_night_window", lambda: False)
    health = iter([False, True])
    monkeypatch.setattr(qwen_runtime, "_health", lambda *a, **k: next(health))
    calls, prints = [], [0]

    def launchctl(*args):
        calls.append(args)
        if args[0] == "print":
            prints[0] += 1
            return SimpleNamespace(returncode=0 if prints[0] == 1 else 113,
                                   stdout="state = SIGTERMed")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(qwen_runtime, "_launchctl", launchctl)
    with qwen_runtime.channel_runtime(cfg()):
        pass
    assert [args[0] for args in calls] == ["print", "print", "bootstrap", "bootout", "print"]
