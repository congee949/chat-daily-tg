#!/usr/bin/env python3
"""schedule.py — 从 schedule.yaml 一键调整 Mac 上 4 个 launchd label 的触发时间。

单一事实源是仓库根的 schedule.yaml。apply 的动作链（与 install-launchd.sh 同款）：
  1. 把每个 label 的时间写回 launchd/<label>.plist 模板（保留注释与占位符，进 git）；
  2. 渲染 REPLACE_WITH_* 占位符到 ~/Library/LaunchAgents/<label>.plist；
  3. launchctl unload/load 重载。

重载前有两道闸门（避免误杀在飞行中的 run，2026-07-18 事故后加）：
  - 幂等：已装 plist 与将写入的逐字节相同 → 跳过该 label 的重载；
  - in-flight：`launchctl list` 报活跃 PID（或状态拿不准）→ 默认跳过重载并告警，
    退出码非 0；确认要强杀才加 --force。

只管 Mac。r4s 的 B站 digest（cron 每小时 :30）不在此工具范围。

用法：
  python scripts/schedule.py list          # 对比 yaml 与已装 plist 的当前时间
  python scripts/schedule.py apply          # 写模板 + 重装 + reload（跳过运行中的 label）
  python scripts/schedule.py apply -n       # dry-run：只打印将写入的时间，不落盘
  python scripts/schedule.py apply --force   # 即使 job 在运行也重载（会 SIGTERM 杀掉它）
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT / "launchd"
INSTALL_DIR = Path.home() / "Library" / "LaunchAgents"
CONFIG = PROJECT / "schedule.yaml"

LABELS = {
    "agent": "com.chat-daily-tg.agent",
    "channels": "com.chat-daily-tg.channels",
    "growth": "com.chat-daily-tg.growth",
    "growth-weekly": "com.chat-daily-tg.growth-weekly",
}


# ── config parsing ──────────────────────────────────────────────────────────
def _hm(value) -> tuple[int, int]:
    """'HH:MM' 或纯小时数字 → (hour, minute)。"""
    if isinstance(value, int):
        return value, 0
    s = str(value).strip()
    if ":" in s:
        h, m = s.split(":", 1)
        return int(h), int(m)
    return int(s), 0


def load_config() -> dict:
    try:
        import yaml
    except ModuleNotFoundError:
        sys.exit("需要 PyYAML：cd 到项目后 `uv sync` 或 `pip install pyyaml`")
    if not CONFIG.exists():
        sys.exit(f"找不到配置：{CONFIG}")
    with CONFIG.open() as f:
        return yaml.safe_load(f) or {}


def entries_for(key: str, cfg: dict) -> list[dict]:
    """把某 label 的配置归一化为 launchd StartCalendarInterval 条目列表。"""
    node = cfg.get(key)
    if key == "agent":
        h, m = _hm(node["trigger"])
        return [{"Hour": h, "Minute": m}]
    if key in ("channels", "growth"):
        out = []
        for item in node:
            h, m = _hm(item)
            out.append({"Hour": h, "Minute": m})
        return out
    if key == "growth-weekly":
        h, m = _hm(node["time"])
        return [{"Weekday": int(node["weekday"]), "Hour": h, "Minute": m}]
    raise KeyError(key)


# ── plist surgery（字符串手术，保留模板里的注释）─────────────────────────────
def _entry_xml(e: dict) -> str:
    parts = []
    for k in ("Weekday", "Hour", "Minute"):
        if k in e:
            parts.append(f"<key>{k}</key><integer>{e[k]}</integer>")
    return "\t\t<dict>" + "".join(parts) + "</dict>"


def set_calendar(text: str, entries: list[dict]) -> str:
    """替换 StartCalendarInterval 数组里的 <dict> 条目，保留其中的 <!-- 注释 -->。"""
    m = re.search(
        r"(<key>StartCalendarInterval</key>\s*<array>)(.*?)(</array>)", text, re.S
    )
    if not m:
        raise ValueError("模板缺少 StartCalendarInterval")
    comments = re.findall(r"[ \t]*<!--.*?-->", m.group(2), re.S)
    inner = "\n"
    for c in comments:
        inner += c.rstrip() + "\n"
    inner += "\n".join(_entry_xml(e) for e in entries) + "\n\t"
    return text[: m.start(2)] + inner + text[m.end(2) :]


def set_env_var(text: str, name: str, value: str) -> str:
    """在 EnvironmentVariables dict 里增/改一个键（保留 PATH 等既有键）。"""
    m = re.search(
        r"(<key>EnvironmentVariables</key>\s*<dict>)(.*?)(\n\t</dict>)", text, re.S
    )
    if not m:
        raise ValueError("模板缺少 EnvironmentVariables")
    body = m.group(2)
    pair_re = re.compile(
        rf"(\n\t\t<key>{re.escape(name)}</key>\s*<string>).*?(</string>)", re.S
    )
    if pair_re.search(body):
        body = pair_re.sub(rf"\g<1>{value}\g<2>", body)
    else:
        body += f"\n\t\t<key>{name}</key>\n\t\t<string>{value}</string>"
    return text[: m.start(2)] + body + text[m.end(2) :]


def render_placeholders(text: str) -> str:
    return (
        text.replace("REPLACE_WITH_HOME", os.environ["HOME"])
        .replace("REPLACE_WITH_PROJECT_DIR", str(PROJECT))
        .replace("REPLACE_WITH_DATA_DIR", os.environ.get("CHAT_DAILY_DATA_DIR", str(Path.home() / "chat-daily")))
    )


# ── inspection ──────────────────────────────────────────────────────────────
def installed_entries(label: str) -> list[dict] | None:
    p = INSTALL_DIR / f"{label}.plist"
    if not p.exists():
        return None
    # 模板注释里含 `--wait-for-wake`（双连字符），严格 XML 不允许注释含 `--`，
    # launchctl 宽容但 plistlib/expat 会拒。剥掉注释再解析。
    cleaned = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.S)
    data = plistlib.loads(cleaned.encode())
    sci = data.get("StartCalendarInterval")
    if isinstance(sci, dict):
        sci = [sci]
    return sci or []


def fmt_entries(entries: list[dict]) -> str:
    def one(e):
        t = f"{e.get('Hour', 0):02d}:{e.get('Minute', 0):02d}"
        return f"周{e['Weekday']} {t}" if "Weekday" in e else t
    return ", ".join(one(e) for e in entries)


def job_running(label: str) -> bool | None:
    """该 label 的 job 当前是否正在飞行？

    返回三态，让调用方分辨"确定安全"与"拿不准"：
      True  — `launchctl list` 报了活跃 PID，正在跑，unload 会 SIGTERM 它；
      False — 域里没这个服务（从没跑过 / 已 bootout），或已加载但无 PID（idle），
              两种都安全重载；
      None  — launchctl 缺失、抛错、或输出反常无法解析 → 拿不准。

    对抗式取舍：宁可 None（保守跳过重载）也不误判成 False 去杀在飞行的 run。
    """
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None  # launchctl 不在 PATH / 无权限 → 拿不准
    if r.returncode != 0:
        return False  # "Could not find service ... in domain" → 未加载，没在跑
    if re.search(r'"PID"\s*=\s*\d+;', r.stdout):
        return True
    # returncode 0 但输出既无 PID 也不像 plist dict → 反常，别当 idle 处理
    if '"Label"' not in r.stdout:
        return None
    return False  # 已加载但无 PID = idle，安全重载


# ── commands ────────────────────────────────────────────────────────────────
def cmd_list(cfg: dict) -> int:
    print("label            yaml 配置                          已装 plist")
    print("─" * 78)
    for key, label in LABELS.items():
        want = fmt_entries(entries_for(key, cfg))
        have = installed_entries(label)
        have_s = "（未安装）" if have is None else fmt_entries(have)
        mark = "" if have is not None and want == have_s else "  ← 待 apply"
        extra = f"  deadline={cfg['agent']['deadline']}" if key == "agent" else ""
        print(f"{key:<16} {want:<34} {have_s}{mark}{extra}")
    return 0


def cmd_apply(cfg: dict, dry_run: bool, force: bool = False) -> int:
    rc = 0
    for key, label in LABELS.items():
        tpl = TEMPLATE_DIR / f"{label}.plist"
        text = tpl.read_text()
        text = set_calendar(text, entries_for(key, cfg))
        if key == "agent":
            text = set_env_var(text, "CHAT_DAILY_WAKE_DEADLINE", str(cfg["agent"]["deadline"]))
        summary = (fmt_entries(entries_for(key, cfg))
                   + (f"  deadline={cfg['agent']['deadline']}" if key == "agent" else ""))

        if dry_run:
            print(f"[dry-run] {key}: {summary}")
            continue

        tpl.write_text(text)  # 模板始终跟随 yaml（进 git），与是否重载无关
        dst = INSTALL_DIR / f"{label}.plist"
        rendered = render_placeholders(text)

        # 幂等：已装 plist 逐字节相同 → 无变化，不重载（从根上少一次误杀机会）
        if dst.exists() and dst.read_text() == rendered:
            print(f"= 未变化，跳过重载  {key}: {summary}")
            continue

        # in-flight 保护：unload 会给正在跑的 job 发 SIGTERM，除非 --force 否则不碰
        running = job_running(label)
        if running is not False and not force:
            reason = "正在运行" if running is True else "运行状态无法确定"
            print(f"⚠ {key} {reason}，跳过重载以免杀掉在飞行中的 run；"
                  f"稍后无 run 时重跑 apply（确认要强杀加 --force）")
            rc = 1
            continue

        dst.write_text(rendered)
        subprocess.run(["launchctl", "unload", str(dst)],
                       stderr=subprocess.DEVNULL, check=False)
        r = subprocess.run(["launchctl", "load", str(dst)], check=False)
        if r.returncode == 0:
            note = "✓ reloaded（--force 强杀）" if (force and running is not False) else "✓ reloaded"
        else:
            note = f"✗ launchctl load exit={r.returncode}"
            rc = 1
        print(f"{note}  {key}: {summary}")

    if dry_run:
        print("\n（dry-run，未写盘、未 reload）")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="从 schedule.yaml 调整 Mac launchd 触发时间")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="对比 yaml 与已装 plist 的当前时间")
    ap_apply = sub.add_parser("apply", help="写模板 + 重装 + reload")
    ap_apply.add_argument("-n", "--dry-run", action="store_true", help="只打印，不写盘")
    ap_apply.add_argument(
        "-f", "--force", "--force-running", dest="force", action="store_true",
        help="即使 job 正在运行也重载（会 SIGTERM 杀掉在飞行中的 run）")
    args = ap.parse_args()

    cfg = load_config()
    if args.cmd == "apply":
        return cmd_apply(cfg, args.dry_run, args.force)
    return cmd_list(cfg)  # 默认与 list 等价


if __name__ == "__main__":
    sys.exit(main())
