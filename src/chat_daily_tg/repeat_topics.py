from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Iterator, Literal

from chat_daily_tg.sqlite_util import connect


TopicStatus = Literal["active", "dormant"]

_NORMALIZE_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_topic_title(title: str) -> str:
    return _NORMALIZE_RE.sub("", title.lower())


def topic_id(title: str) -> str:
    normalized = normalize_topic_title(title)
    return sha256(normalized.encode("utf-8")).hexdigest()[:12]


@dataclass
class TopicMention:
    title: str
    summary: str
    source_group: str = ""
    source_sender: str = ""
    has_new_information: bool = False
    new_information: str | None = None


@dataclass
class RepeatTopic:
    id: str
    title: str
    first_seen: str
    last_seen: str
    seen_dates: list[str]
    mention_count: int
    last_summary: str
    status: TopicStatus = "active"
    last_source_group: str = ""
    last_source_sender: str = ""
    last_new_information: str | None = None

    @property
    def consecutive_days(self) -> int:
        dates = sorted({date.fromisoformat(d) for d in self.seen_dates})
        if not dates:
            return 0
        streak = 1
        cursor = dates[-1]
        for prev in reversed(dates[:-1]):
            if (cursor - prev).days == 1:
                streak += 1
                cursor = prev
            else:
                break
        return streak

    def is_recent(self, today: str, cooldown_days: int = 7) -> bool:
        return (date.fromisoformat(today) - date.fromisoformat(self.last_seen)).days <= cooldown_days

    def is_repeat(self) -> bool:
        return self.mention_count >= 2 or self.consecutive_days >= 2


_FIELDS = (
    "id", "title", "first_seen", "last_seen", "seen_dates", "mention_count",
    "last_summary", "status", "last_source_group", "last_source_sender",
    "last_new_information",
)


def _row_to_topic(row) -> RepeatTopic:
    try:
        seen = json.loads(row["seen_dates"]) if row["seen_dates"] else []
    except (ValueError, TypeError):
        seen = []
    return RepeatTopic(
        id=row["id"], title=row["title"], first_seen=row["first_seen"],
        last_seen=row["last_seen"], seen_dates=list(seen),
        mention_count=row["mention_count"], last_summary=row["last_summary"],
        status=row["status"], last_source_group=row["last_source_group"],
        last_source_sender=row["last_source_sender"],
        last_new_information=row["last_new_information"],
    )


class RepeatTopicDB:
    def __init__(self, path: Path):
        self.path = path

    def _conn(self):
        return connect(self.path)

    def read_all(self) -> Iterator[RepeatTopic]:
        conn = self._conn()
        try:
            for row in conn.execute("SELECT * FROM repeat_topics ORDER BY rowid"):
                yield _row_to_topic(row)
        finally:
            conn.close()

    @staticmethod
    def _write(conn, topic: RepeatTopic) -> None:
        data = {
            "id": topic.id, "title": topic.title, "first_seen": topic.first_seen,
            "last_seen": topic.last_seen,
            "seen_dates": json.dumps(topic.seen_dates, ensure_ascii=False),
            "mention_count": topic.mention_count, "last_summary": topic.last_summary,
            "status": topic.status, "last_source_group": topic.last_source_group,
            "last_source_sender": topic.last_source_sender,
            "last_new_information": topic.last_new_information,
        }
        placeholders = ", ".join(f":{c}" for c in _FIELDS)
        conn.execute(
            f"INSERT INTO repeat_topics ({', '.join(_FIELDS)}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in _FIELDS if c != "id"),
            data,
        )

    def upsert_many(self, mentions: list[TopicMention], seen_date: str) -> list[RepeatTopic]:
        conn = self._conn()
        try:
            by_id: dict[str, RepeatTopic] = {}
            for row in conn.execute("SELECT * FROM repeat_topics"):
                t = _row_to_topic(row)
                by_id[t.id] = t
            updated: list[RepeatTopic] = []
            touched: list[RepeatTopic] = []
            for mention in mentions:
                title = mention.title.strip()
                if not title:
                    continue
                tid = topic_id(title)
                if tid in by_id:
                    topic = by_id[tid]
                    if seen_date not in topic.seen_dates:
                        topic.seen_dates.append(seen_date)
                        topic.seen_dates.sort()
                        topic.mention_count += 1
                    topic.title = title
                    topic.last_seen = max(topic.last_seen, seen_date)
                    topic.last_summary = mention.summary or topic.last_summary
                    topic.last_source_group = mention.source_group or topic.last_source_group
                    topic.last_source_sender = mention.source_sender or topic.last_source_sender
                    if mention.has_new_information and mention.new_information:
                        topic.last_new_information = mention.new_information
                    topic.status = "active"
                else:
                    topic = RepeatTopic(
                        id=tid,
                        title=title,
                        first_seen=seen_date,
                        last_seen=seen_date,
                        seen_dates=[seen_date],
                        mention_count=1,
                        last_summary=mention.summary,
                        last_source_group=mention.source_group,
                        last_source_sender=mention.source_sender,
                        last_new_information=mention.new_information if mention.has_new_information else None,
                    )
                    by_id[tid] = topic
                updated.append(topic)
                touched.append(topic)
            with conn:
                for topic in touched:
                    self._write(conn, topic)
            return updated
        finally:
            conn.close()


def mentions_from_json(items: list[dict]) -> list[TopicMention]:
    mentions: list[TopicMention] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        mentions.append(TopicMention(
            title=title,
            summary=str(item.get("summary") or "").strip(),
            source_group=str(item.get("source_group") or "").strip(),
            source_sender=str(item.get("source_sender") or "").strip(),
            has_new_information=bool(item.get("has_new_information")),
            new_information=item.get("new_information"),
        ))
    return mentions


def recent_repeat_summary(path: Path, today: str, cooldown_days: int = 7, max_items: int = 30) -> str:
    db = RepeatTopicDB(path)
    lines: list[str] = []
    for topic in db.read_all():
        if not topic.is_recent(today, cooldown_days=cooldown_days):
            continue
        marker = "repeat" if topic.is_repeat() else "seen_once"
        lines.append(
            f"- `{topic.id}` [{marker}] {topic.title} | last_seen={topic.last_seen} "
            f"| mentions={topic.mention_count} | streak={topic.consecutive_days} | {topic.last_summary}"
        )
        if len(lines) >= max_items:
            break
    return "\n".join(lines) if lines else "(空)"
