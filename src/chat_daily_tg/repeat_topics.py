from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Iterator, Literal


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


class RepeatTopicDB:
    def __init__(self, path: Path):
        self.path = path

    def _ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def read_all(self) -> Iterator[RepeatTopic]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield RepeatTopic(**json.loads(line))

    def _rewrite(self, topics: list[RepeatTopic]) -> None:
        self._ensure()
        with open(self.path, "w", encoding="utf-8") as f:
            for topic in topics:
                f.write(json.dumps(asdict(topic), ensure_ascii=False) + "\n")

    def upsert_many(self, mentions: list[TopicMention], seen_date: str) -> list[RepeatTopic]:
        topics = list(self.read_all())
        by_id = {topic.id: topic for topic in topics}
        updated: list[RepeatTopic] = []
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
                topics.append(topic)
                by_id[tid] = topic
            updated.append(topic)
        self._rewrite(topics)
        return updated


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
