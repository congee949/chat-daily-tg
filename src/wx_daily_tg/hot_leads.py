from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, timedelta
import json
from pathlib import Path


@dataclass
class HotLead:
    id: str
    captured_at: str           # YYYY-MM-DD
    title: str
    summary: str
    category: str              # arbitrage | bug | personal_trick | gray_zone
    source_group: str
    source_sender: str
    status: str                # alive | likely_dead | dead
    risk_notes: str | None = None
    death_signal: str | None = None


def _day_file(root: Path, date_str: str) -> Path:
    y, m, d = date_str.split("-")
    return root / y / m / f"{d}.md"


def _day_jsonl(root: Path, date_str: str) -> Path:
    """Internal storage: every day's new leads stored as JSONL alongside md."""
    y, m, d = date_str.split("-")
    return root / y / m / f"{d}.jsonl"


def append_day_leads(root: Path, date_str: str, leads: list[HotLead]) -> Path | None:
    """Write that day's new hot leads to YYYY/MM/DD.md and .jsonl.
    Returns md path if anything was written, else None.
    """
    if not leads:
        return None
    md = _day_file(root, date_str)
    jl = _day_jsonl(root, date_str)
    md.parent.mkdir(parents=True, exist_ok=True)

    md_lines = [f"# {date_str} 热点板新增", ""]
    for lead in leads:
        md_lines.extend([
            f"## {lead.title}",
            f"- 出处：{lead.source_group} / {lead.source_sender}",
            f"- 分类：{lead.category}",
            f"- 摘要：{lead.summary}",
            f"- 状态：{lead.status}",
            f"- ID：`{lead.id}`",
            "",
        ])
        if lead.risk_notes:
            md_lines.insert(-1, f"- 风险：{lead.risk_notes}")
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # JSONL
    with open(jl, "a", encoding="utf-8") as f:
        for lead in leads:
            f.write(json.dumps(asdict(lead), ensure_ascii=False) + "\n")
    return md


def load_all_leads(root: Path) -> list[HotLead]:
    """Walk all YYYY/MM/DD.jsonl and load."""
    out: list[HotLead] = []
    if not root.exists():
        return out
    for jl in sorted(root.rglob("*.jsonl")):
        with open(jl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(HotLead(**json.loads(line)))
    return out


def regenerate_latest(root: Path, latest_md: Path, retention_days: int = 14) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    leads = load_all_leads(root)
    active = [
        l for l in leads
        if l.status == "alive"
        and date.fromisoformat(l.captured_at) >= cutoff
    ]
    by_cat: dict[str, list[HotLead]] = {}
    for l in active:
        by_cat.setdefault(l.category, []).append(l)

    lines = [
        "# 热点板 — 活跃机会",
        f"> 保留窗口：{retention_days} 天；自动生成，改 YYYY/MM/DD.jsonl 不改这里",
        "",
    ]
    for cat, items in sorted(by_cat.items()):
        lines.append(f"## {cat}")
        lines.append("")
        for l in sorted(items, key=lambda x: x.captured_at, reverse=True):
            lines.extend([
                f"### {l.title} ({l.captured_at})",
                f"- 来源：{l.source_group} / {l.source_sender}",
                f"- 摘要：{l.summary}",
            ])
            if l.risk_notes:
                lines.append(f"- 风险：{l.risk_notes}")
            lines.append(f"- ID：`{l.id}`")
            lines.append("")

    latest_md.parent.mkdir(parents=True, exist_ok=True)
    latest_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mark_lead_status(root: Path, lead_id: str, status: str,
                      death_signal: str | None = None) -> bool:
    """Find a lead by id across all day-jsonl files and update its status."""
    if not root.exists():
        return False
    found = False
    for jl in root.rglob("*.jsonl"):
        leads = []
        modified = False
        with open(jl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("id") == lead_id:
                    data["status"] = status
                    if death_signal is not None:
                        data["death_signal"] = death_signal
                    modified = True
                    found = True
                leads.append(data)
        if modified:
            with open(jl, "w", encoding="utf-8") as f:
                for data in leads:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return found
