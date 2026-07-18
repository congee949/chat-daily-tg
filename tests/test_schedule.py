"""scripts/schedule.py 的 in-flight 保护与幂等跳过测试。

重点：`apply` 绝不能 unload 一个正在飞行的 launchd job（会 SIGTERM 杀掉它，
2026-07-18 真实事故）。这里 mock `subprocess.run`，模拟 `launchctl list` 的各种
输出，验证"检测到运行中→跳过"、"未运行→正常重载"、`--force` 覆盖、幂等跳过，
以及解析异常时保守跳过。
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "schedule_under_test", PROJECT / "scripts" / "schedule.py"
)
sched = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sched)


# 一份最小可用的 channels 模板（含 StartCalendarInterval 数组即可）。
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>com.test.channels</string>
\t<key>StartCalendarInterval</key>
\t<array>
\t\t<dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
\t</array>
</dict>
</plist>
"""


class FakeLaunchctl:
    """可编程的 subprocess.run 替身。

    list_result: 对 `launchctl list <label>` 返回的 (returncode, stdout)，
                 或一个 raise 的 Exception 实例。
    calls: 记录所有收到的 argv，供断言 unload/load 是否发生。
    """

    def __init__(self, list_result=(0, "")):
        self.list_result = list_result
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "list"]:
            if isinstance(self.list_result, Exception):
                raise self.list_result
            rc, out = self.list_result
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        # unload / load 一律成功
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def did(self, verb: str) -> bool:
        return any(c[:2] == ["launchctl", verb] for c in self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 schedule 模块指向 tmp 目录里的单个 channels label。"""
    tpl_dir = tmp_path / "launchd"
    inst_dir = tmp_path / "LaunchAgents"
    tpl_dir.mkdir()
    inst_dir.mkdir()
    (tpl_dir / "com.test.channels.plist").write_text(TEMPLATE)

    monkeypatch.setattr(sched, "TEMPLATE_DIR", tpl_dir)
    monkeypatch.setattr(sched, "INSTALL_DIR", inst_dir)
    monkeypatch.setattr(sched, "LABELS", {"channels": "com.test.channels"})
    cfg = {"channels": [6, 10]}
    return SimpleNamespace(cfg=cfg, tpl_dir=tpl_dir, inst_dir=inst_dir,
                           dst=inst_dir / "com.test.channels.plist")


# ── job_running 解析 ─────────────────────────────────────────────────────────
def test_job_running_active_pid(monkeypatch):
    fake = FakeLaunchctl((0, '{\n\t"PID" = 10995;\n\t"Label" = "x";\n};'))
    monkeypatch.setattr(sched.subprocess, "run", fake)
    assert sched.job_running("x") is True


def test_job_running_loaded_but_idle(monkeypatch):
    fake = FakeLaunchctl((0, '{\n\t"Label" = "x";\n\t"OnDemand" = true;\n};'))
    monkeypatch.setattr(sched.subprocess, "run", fake)
    assert sched.job_running("x") is False


def test_job_running_not_loaded(monkeypatch):
    # `launchctl list <label>` 对未加载的服务返回非 0
    fake = FakeLaunchctl((113, ""))
    monkeypatch.setattr(sched.subprocess, "run", fake)
    assert sched.job_running("x") is False


def test_job_running_garbage_output_is_indeterminate(monkeypatch):
    # returncode 0 但输出既无 PID 也不像 plist → 拿不准，保守返回 None
    fake = FakeLaunchctl((0, "wat"))
    monkeypatch.setattr(sched.subprocess, "run", fake)
    assert sched.job_running("x") is None


def test_job_running_launchctl_missing_is_indeterminate(monkeypatch):
    fake = FakeLaunchctl(FileNotFoundError("launchctl"))
    monkeypatch.setattr(sched.subprocess, "run", fake)
    assert sched.job_running("x") is None


# ── cmd_apply 决策 ───────────────────────────────────────────────────────────
def test_apply_skips_running_job(env, monkeypatch, capsys):
    fake = FakeLaunchctl((0, '{\n\t"PID" = 10995;\n};'))
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False)

    assert rc == 1  # 有 label 没应用 → 非 0
    assert not fake.did("unload") and not fake.did("load")
    assert not env.dst.exists()  # 没写 live plist
    assert "跳过重载" in capsys.readouterr().out


def test_apply_reloads_when_not_running(env, monkeypatch):
    fake = FakeLaunchctl((0, '{\n\t"Label" = "x";\n};'))  # idle
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False)

    assert rc == 0
    assert fake.did("unload") and fake.did("load")
    assert env.dst.exists()


def test_apply_reloads_when_not_installed(env, monkeypatch):
    # 服务从未加载（list 非 0）→ 安全，正常首装
    fake = FakeLaunchctl((113, ""))
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False)

    assert rc == 0
    assert fake.did("load")


def test_apply_force_overrides_running(env, monkeypatch, capsys):
    fake = FakeLaunchctl((0, '{\n\t"PID" = 10995;\n};'))
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False, force=True)

    assert rc == 0
    assert fake.did("unload") and fake.did("load")
    assert "强杀" in capsys.readouterr().out


def test_apply_indeterminate_skips_without_force(env, monkeypatch, capsys):
    fake = FakeLaunchctl((0, "garbage"))  # job_running → None
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False)

    assert rc == 1
    assert not fake.did("unload") and not fake.did("load")
    assert "无法确定" in capsys.readouterr().out


def test_apply_idempotent_skip_when_unchanged(env, monkeypatch, capsys):
    # 预置一份与将写入内容逐字节相同的 live plist
    tpl_text = (env.tpl_dir / "com.test.channels.plist").read_text()
    rendered = sched.render_placeholders(
        sched.set_calendar(tpl_text, sched.entries_for("channels", env.cfg))
    )
    env.dst.write_text(rendered)

    fake = FakeLaunchctl((0, '{\n\t"PID" = 10995;\n};'))  # 就算在跑也无所谓
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=False)

    assert rc == 0
    # 未变化：连 list 探测都不必，更不该 unload/load
    assert not fake.did("unload") and not fake.did("load")
    assert "未变化" in capsys.readouterr().out


def test_apply_dry_run_touches_nothing(env, monkeypatch):
    fake = FakeLaunchctl((0, '{\n\t"PID" = 1;\n};'))
    monkeypatch.setattr(sched.subprocess, "run", fake)

    rc = sched.cmd_apply(env.cfg, dry_run=True)

    assert rc == 0
    assert not fake.calls  # dry-run 不碰 launchctl
    assert not env.dst.exists()
