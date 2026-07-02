from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from chat_daily_tg.sqlite_util import connect


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


_FIELDS = (
    "id", "captured_at", "title", "summary", "category", "source_group",
    "source_sender", "status", "risk_notes", "death_signal",
)


def _row_to_lead(row) -> HotLead:
    return HotLead(**{k: row[k] for k in _FIELDS})


def _day_file(md_root: Path, date_str: str) -> Path:
    y, m, d = date_str.split("-")
    return md_root / y / m / f"{d}.md"


def _write_day_md(md_root: Path, date_str: str, leads: list[HotLead]) -> Path:
    md = _day_file(md_root, date_str)
    md.parent.mkdir(parents=True, exist_ok=True)
    md_lines = [f"# {date_str} 热点板新增", ""]
    for lead in leads:
        block = [
            f"## {lead.title}",
            f"- 出处：{lead.source_group} / {lead.source_sender}",
            f"- 分类：{lead.category}",
            f"- 摘要：{lead.summary}",
            f"- 状态：{lead.status}",
        ]
        if lead.risk_notes:
            block.append(f"- 风险：{lead.risk_notes}")
        block.extend([f"- ID：`{lead.id}`", ""])
        md_lines.extend(block)
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return md


def append_day_leads(
    db_path: Path, date_str: str, leads: list[HotLead], md_root: Path | None = None
) -> Path | None:
    """Persist that day's new hot leads to the DB (upsert by id, idempotent on
    rerun). Optionally also write a human-readable YYYY/MM/DD.md under md_root.

    Returns the md path if one was written, else None.
    """
    if not leads:
        return None
    conn = connect(db_path)
    try:
        placeholders = ", ".join(f":{c}" for c in _FIELDS)
        sql = (
            f"INSERT INTO hot_leads ({', '.join(_FIELDS)}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _FIELDS if c != "id")
        )
        with conn:
            for lead in leads:
                conn.execute(sql, {
                    "id": lead.id, "captured_at": lead.captured_at,
                    "title": lead.title, "summary": lead.summary,
                    "category": lead.category, "source_group": lead.source_group,
                    "source_sender": lead.source_sender, "status": lead.status,
                    "risk_notes": lead.risk_notes, "death_signal": lead.death_signal,
                })
    finally:
        conn.close()
    if md_root is not None:
        return _write_day_md(md_root, date_str, leads)
    return None


def load_all_leads(db_path: Path) -> list[HotLead]:
    """Load every stored hot lead."""
    if not Path(db_path).exists():
        return []
    conn = connect(db_path)
    try:
        return [_row_to_lead(r) for r in conn.execute(
            "SELECT * FROM hot_leads ORDER BY rowid")]
    finally:
        conn.close()


def regenerate_latest(db_path: Path, latest_md: Path, retention_days: int = 14) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    leads = load_all_leads(db_path)
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
        f"> 保留窗口：{retention_days} 天；自动生成，改数据库不改这里",
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


def mark_lead_status(db_path: Path, lead_id: str, status: str,
                     death_signal: str | None = None) -> bool:
    """Update a lead's status by id. Returns True if a row was updated."""
    if not Path(db_path).exists():
        return False
    conn = connect(db_path)
    try:
        with conn:
            if death_signal is not None:
                cur = conn.execute(
                    "UPDATE hot_leads SET status = ?, death_signal = ? WHERE id = ?",
                    (status, death_signal, lead_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE hot_leads SET status = ? WHERE id = ?",
                    (status, lead_id),
                )
            return cur.rowcount > 0
    finally:
        conn.close()
