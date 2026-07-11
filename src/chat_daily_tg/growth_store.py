from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import logging
import re
import sqlite3

from chat_daily_tg.sqlite_util import connect

log = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# A new segment whose msg-id span overlaps an existing one (any status) by at
# least this ratio — overlap width / the narrower span's width — is treated as
# a re-mine of the same conversation and dropped. Id-width is an approximation
# of message count (ids aren't dense), but both sides are measured the same way.
OVERLAP_DUP_RATIO = 0.5


@dataclass
class GrowthSegment:
    """One mined conversation segment. `quotes` items are
    {"msg_id": int, "sender": str, "text": str} and must already have passed
    the miner's verbatim check before insert."""
    id: str                      # "<date>-<start_msg_id>"
    chat_id: int                 # positive DB form (e.g. 1162433032)
    chat_name: str
    date: str                    # Beijing YYYY-MM-DD the segment belongs to
    start_msg_id: int
    end_msg_id: int
    start_hm: str                # Beijing HH:MM
    end_hm: str
    msg_count: int
    theme: str
    points: list[str] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)
    participants: str = ""       # comma-joined display names
    score: float = 0.0
    status: str = "pending"      # pending | sent | rejected
    mined_at: str = ""
    sent_at: str | None = None
    sent_style: str | None = None
    slice_path: str | None = None


def segment_id(date_str: str, start_msg_id: int) -> str:
    return f"{date_str}-{start_msg_id}"


def _now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _row_to_segment(row: sqlite3.Row) -> GrowthSegment:
    return GrowthSegment(
        id=row["id"], chat_id=row["chat_id"], chat_name=row["chat_name"],
        date=row["date"], start_msg_id=row["start_msg_id"], end_msg_id=row["end_msg_id"],
        start_hm=row["start_hm"], end_hm=row["end_hm"], msg_count=row["msg_count"],
        theme=row["theme"], points=json.loads(row["points_json"]),
        quotes=json.loads(row["quotes_json"]), participants=row["participants"],
        score=row["score"], status=row["status"], mined_at=row["mined_at"],
        sent_at=row["sent_at"], sent_style=row["sent_style"], slice_path=row["slice_path"],
    )


# ---------------------------------------------------------------- mined days

def day_already_mined(db_path: Path, chat_id: int, date_str: str) -> bool:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM growth_mined_days WHERE chat_id = ? AND date = ?",
            (chat_id, date_str)).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_day_mined(db_path: Path, chat_id: int, date_str: str, segments_found: int) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO growth_mined_days "
                "(chat_id, date, mined_at, segments_found) VALUES (?, ?, ?, ?)",
                (chat_id, date_str, _now_iso(), segments_found))
    finally:
        conn.close()


def mined_days_summary(db_path: Path, chat_id: int) -> dict:
    """Backfill progress for the weekly report."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last "
            "FROM growth_mined_days WHERE chat_id = ?", (chat_id,)).fetchone()
        return {"days": row["n"], "first": row["first"], "last": row["last"]}
    finally:
        conn.close()


# ------------------------------------------------------------------ segments

def _overlap_ratio(s1: int, e1: int, s2: int, e2: int) -> float:
    overlap = min(e1, e2) - max(s1, s2) + 1
    if overlap <= 0:
        return 0.0
    narrower = min(e1 - s1 + 1, e2 - s2 + 1)
    return overlap / narrower


def insert_segments(db_path: Path, segs: list[GrowthSegment]) -> list[GrowthSegment]:
    """Insert with overlap dedup against every stored segment of the same chat
    (any status — a rejected span must not resurface as a near-duplicate).
    Returns only the segments that were actually inserted."""
    if not segs:
        return []
    inserted: list[GrowthSegment] = []
    conn = connect(db_path)
    try:
        with conn:
            for seg in segs:
                clash = None
                for row in conn.execute(
                    "SELECT id, start_msg_id, end_msg_id FROM growth_segments "
                    "WHERE chat_id = ? AND NOT (end_msg_id < ? OR start_msg_id > ?)",
                    (seg.chat_id, seg.start_msg_id, seg.end_msg_id),
                ):
                    ratio = _overlap_ratio(seg.start_msg_id, seg.end_msg_id,
                                           row["start_msg_id"], row["end_msg_id"])
                    if ratio >= OVERLAP_DUP_RATIO:
                        clash = (row["id"], ratio)
                        break
                if clash is not None:
                    log.info("growth segment %s dropped: overlaps %s at %.0f%%",
                             seg.id, clash[0], clash[1] * 100)
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO growth_segments "
                    "(id, chat_id, chat_name, date, start_msg_id, end_msg_id, "
                    " start_hm, end_hm, msg_count, theme, points_json, quotes_json, "
                    " participants, score, status, mined_at, sent_at, sent_style, slice_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (seg.id, seg.chat_id, seg.chat_name, seg.date,
                     seg.start_msg_id, seg.end_msg_id, seg.start_hm, seg.end_hm,
                     seg.msg_count, seg.theme, json.dumps(seg.points, ensure_ascii=False),
                     json.dumps(seg.quotes, ensure_ascii=False), seg.participants,
                     seg.score, seg.status, seg.mined_at or _now_iso(),
                     seg.sent_at, seg.sent_style, seg.slice_path))
                if cur.rowcount:
                    inserted.append(seg)
                else:
                    log.info("growth segment %s dropped: exact span already stored", seg.id)
    finally:
        conn.close()
    return inserted


def pick_next(db_path: Path, prefer_date: str) -> GrowthSegment | None:
    """Today's freshly-mined segments first (best score); otherwise drain the
    backlog best-score-first, newest day breaking ties."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM growth_segments WHERE status = 'pending' AND date = ? "
            "ORDER BY score DESC, start_msg_id ASC LIMIT 1", (prefer_date,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM growth_segments WHERE status = 'pending' "
                "ORDER BY score DESC, date DESC, start_msg_id ASC LIMIT 1").fetchone()
        return _row_to_segment(row) if row is not None else None
    finally:
        conn.close()


def mark_sent(db_path: Path, seg_id: str, style: str, sent_at: str | None = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE growth_segments SET status = 'sent', sent_at = ?, sent_style = ? "
                "WHERE id = ?", (sent_at or _now_iso(), style, seg_id))
    finally:
        conn.close()


def sent_count_on(db_path: Path, date_str: str) -> int:
    """Cards already pushed on a given Beijing day (daily-quota guard)."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM growth_segments "
            "WHERE status = 'sent' AND sent_at LIKE ?", (f"{date_str}%",)).fetchone()
        return row["n"]
    finally:
        conn.close()


def queue_stats(db_path: Path) -> dict:
    conn = connect(db_path)
    try:
        stats = {"pending": 0, "sent": 0, "rejected": 0}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM growth_segments GROUP BY status"):
            stats[row["status"]] = row["n"]
        return stats
    finally:
        conn.close()


def recent_sent(db_path: Path, days: int = 28) -> list[GrowthSegment]:
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM growth_segments WHERE status = 'sent' AND sent_at >= ? "
            "ORDER BY sent_at DESC", (cutoff,)).fetchall()
        return [_row_to_segment(r) for r in rows]
    finally:
        conn.close()


# -------------------------------------------------------------------- A/B log

def log_ab(db_path: Path, segment_id: str, rubric_version: str, winner: str,
           score_a: float | None, score_b: float | None, verdict: str,
           card_a: str, card_b: str, judged_at: str | None = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO growth_ab_log (segment_id, judged_at, rubric_version, "
                "winner, score_a, score_b, verdict, card_a, card_b) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (segment_id, judged_at or _now_iso(), rubric_version, winner,
                 score_a, score_b, verdict, card_a, card_b))
    finally:
        conn.close()


def ab_stats(db_path: Path, recent_days: int = 7) -> dict:
    """{"total": {"A": n, "B": n}, "recent": {"A": n, "B": n}} win counts.

    Counts one verdict per segment — the LATEST — so a send-retry that re-judged
    the same segment (log_ab is append-only) doesn't inflate win rates. SQLite's
    bare-column-with-MAX semantics makes the inner SELECT pick the max-id row."""
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=recent_days)).isoformat(timespec="seconds")
    latest = ("SELECT segment_id, winner, judged_at, MAX(id) AS mid "
              "FROM growth_ab_log GROUP BY segment_id")
    conn = connect(db_path)
    try:
        stats = {"total": {"A": 0, "B": 0}, "recent": {"A": 0, "B": 0}}
        for row in conn.execute(
                f"SELECT winner, COUNT(*) AS n FROM ({latest}) GROUP BY winner"):
            if row["winner"] in stats["total"]:
                stats["total"][row["winner"]] = row["n"]
        for row in conn.execute(
                f"SELECT winner, COUNT(*) AS n FROM ({latest}) "
                "WHERE judged_at >= ? GROUP BY winner", (cutoff,)):
            if row["winner"] in stats["recent"]:
                stats["recent"][row["winner"]] = row["n"]
        return stats
    finally:
        conn.close()


def latest_ab_pair(db_path: Path) -> sqlite3.Row | None:
    """Most recent judged pair, for the weekly side-by-side sample."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM growth_ab_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


# -------------------------------------------------------------------- rubric

DEFAULT_RUBRIC = """# 成长卡片评审偏好 v1（2026-07-11）

- 拒绝鸡汤和空泛激励：每条要点必须落到具体做法或可检验的判断。
- 金句必须是对话原话，不接受改写、美化或拼接。
- 信息密度优先：宁可短而硬，不要长而软。
- 观点要能迁移到我自己的处境（学生/刚起步），纯个案八卦不要。
- 语气克制，不喊口号，不用感叹号堆情绪。
"""


def ensure_rubric(path: Path) -> tuple[str, str]:
    """Read the judge rubric, creating the built-in v1 on first run.
    Returns (text, version)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_RUBRIC, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    return text, rubric_version_of(text)


def rubric_version_of(text: str) -> str:
    m = re.search(r"\bv(\d+)\b", text.splitlines()[0] if text else "")
    return f"v{m.group(1)}" if m else "v0"


def regenerate_slice_index(db_path: Path, segments_dir: Path) -> Path:
    """Rebuild growth/segments/INDEX.md — one line per archived segment, newest
    day first. The card footer only shows 日期+时段; this index is the fast local
    lookup that maps it (plus theme and msg span) to the slice file. Regenerated
    from the DB each time, so reruns/backfill can never duplicate lines."""
    segments_dir = Path(segments_dir)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT date, start_hm, end_hm, theme, start_msg_id, end_msg_id, "
            "status, slice_path FROM growth_segments WHERE slice_path IS NOT NULL "
            "ORDER BY date DESC, start_msg_id ASC").fetchall()
    finally:
        conn.close()
    lines = [
        "# 成长切片索引",
        "",
        "> 自动生成，改数据库不改这里。卡片尾注只有 日期·时段，靠本表映射到切片。",
        "",
    ]
    for r in rows:
        try:
            rel = str(Path(r["slice_path"]).relative_to(segments_dir))
        except ValueError:
            rel = r["slice_path"]
        mark = "" if r["status"] == "sent" else f" · {r['status']}"
        lines.append(
            f"- {r['date']} {r['start_hm']}–{r['end_hm']} · {r['theme']} · "
            f"msg {r['start_msg_id']}–{r['end_msg_id']}{mark} · [{Path(rel).stem}]({rel})")
    out = segments_dir / "INDEX.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ----------------------------------------------------------------- slice file

def write_slice_file(seg: GrowthSegment, rows: list, out_path: Path) -> Path:
    """Archive the verbatim segment locally so the original conversation stays
    findable. `rows` are messages.db rows (or dicts) covering the span — ALL of
    them, including short/emoji ones the digest would skip: this is an archive,
    not a summary."""
    from chat_daily_tg.telegram_exporter import LOCAL_TZ as TG_TZ, parse_timestamp

    # 不放 t.me 链接：源群消息一天一清，深链必成死链；本切片就是原文的长期载体。
    lines = [
        f"# {seg.theme} — {seg.chat_name}",
        "",
        f"- 日期：{seg.date}（北京 {seg.start_hm}–{seg.end_hm}）",
        f"- span：msg {seg.start_msg_id} – {seg.end_msg_id}（{seg.msg_count} 条）",
        "",
        "---",
        "",
    ]
    for row in rows:
        hm = parse_timestamp(row["timestamp"]).astimezone(TG_TZ).strftime("%H:%M")
        sender = row["sender_name"] or "unknown"
        lines.append(f"[{row['msg_id']}] {hm} {sender}: {row['content'] or ''}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
