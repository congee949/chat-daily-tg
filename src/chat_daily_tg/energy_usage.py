"""Daily closing energy from the Home Assistant Recorder and a local ledger."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from chat_daily_tg.config import EnergyUsage

log = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
UNAVAILABLE = "### ⚡ 小米插座用电\n- 用电数据暂缺"
HISTORY_SQL = """
SELECT state, last_updated_ts FROM states
WHERE metadata_id = ? AND last_updated_ts >= ? AND last_updated_ts <= ?
ORDER BY last_updated_ts, state_id
"""
REMOTE_READER = f"""
import json, sqlite3, sys
uri, entity, start, end = sys.argv[1:]
with sqlite3.connect(uri, uri=True, timeout=5) as db:
    db.execute("PRAGMA query_only=ON")
    meta = db.execute(
        "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity,)
    ).fetchone()
    rows = db.execute({HISTORY_SQL!r}, (meta[0], float(start), float(end))).fetchall() if meta else []
    print(json.dumps(rows))
"""


@dataclass(frozen=True)
class EnergyReading:
    state: str
    timestamp: float


@dataclass(frozen=True)
class DailyEnergy:
    date: str
    kwh: float
    entity_id: str
    last_updated: float


class EnergySource(Protocol):
    def read(self, entity_id: str, start: datetime, end: datetime) -> list[EnergyReading]: ...


def _readonly_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parse_qs(parsed.query).get("mode") != ["ro"]:
        raise ValueError("Recorder URI must use file: and mode=ro")
    return uri


class SQLiteEnergySource:
    """Read a local Recorder snapshot; useful for offline validation."""

    def __init__(self, uri: str):
        self.uri = _readonly_uri(uri)

    def read(self, entity_id: str, start: datetime, end: datetime) -> list[EnergyReading]:
        with closing(sqlite3.connect(self.uri, uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            meta = db.execute(
                "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            if not meta:
                return []
            return [
                EnergyReading(state, timestamp)
                for state, timestamp in db.execute(
                    HISTORY_SQL, (meta[0], start.timestamp(), end.timestamp())
                )
            ]


class SSHEnergySource:
    def __init__(self, cfg: EnergyUsage):
        self.cfg = cfg
        _readonly_uri(cfg.recorder_uri)

    def read(self, entity_id: str, start: datetime, end: datetime) -> list[EnergyReading]:
        command = shlex.join([
            "python3", "-", self.cfg.recorder_uri, entity_id,
            str(start.timestamp()), str(end.timestamp()),
        ])
        result = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o",
                f"ConnectTimeout={max(1, int(self.cfg.timeout_seconds))}",
                "--", self.cfg.ssh_host, command,
            ],
            input=REMOTE_READER, text=True, capture_output=True,
            timeout=self.cfg.timeout_seconds, check=True,
        )
        return [EnergyReading(state, float(timestamp)) for state, timestamp in json.loads(result.stdout)]


def _nonnegative(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def daily_closings(
    readings: list[EnergyReading], start_day: date, end_day: date, entity_id: str,
) -> list[DailyEnergy]:
    """Extend each Beijing day until 00:10, stopping at the next day's reset."""
    valid = []
    for reading in readings:
        value = _nonnegative(reading.state)
        timestamp = _nonnegative(reading.timestamp)
        if value is not None and timestamp is not None:
            valid.append((timestamp, value))
    valid.sort(key=lambda row: row[0])
    records = []
    day = start_day
    while day <= end_day:
        start = datetime.combine(day, time.min, BEIJING).timestamp()
        midnight = datetime.combine(day + timedelta(days=1), time.min, BEIJING).timestamp()
        stop = midnight + 600
        closing_value = None
        for timestamp, value in valid:
            if timestamp < start:
                continue
            if timestamp > stop:
                break
            if timestamp >= midnight:
                # A reset may already be followed by a small positive value.
                # Once the counter drops, none of this new day belongs to D.
                if value == 0 or (closing_value is not None and value < closing_value[1]):
                    break
            closing_value = (timestamp, value)
        if closing_value is not None:
            records.append(DailyEnergy(
                date=day.isoformat(), kwh=closing_value[1],
                entity_id=entity_id, last_updated=closing_value[0],
            ))
        day += timedelta(days=1)
    return records


class EnergyLedger:
    """Persist measured days atomically; an absent date remains absent."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def read(self) -> dict[str, DailyEnergy]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("days"), dict):
            raise ValueError("unsupported energy ledger")
        records = {}
        for key, row in payload["days"].items():
            record = DailyEnergy(**row)
            if (
                record.date != key or date.fromisoformat(key).isoformat() != key
                or _nonnegative(record.kwh) is None
                or _nonnegative(record.last_updated) is None
            ):
                raise ValueError("invalid energy ledger record")
            records[key] = record
        return records

    def update(self, new_records: list[DailyEnergy]) -> dict[str, DailyEnergy]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = self.read()
            merged = dict(existing)
            for record in new_records:
                previous = merged.get(record.date)
                if previous is None or record.last_updated >= previous.last_updated:
                    merged[record.date] = record
            if merged == existing:
                return merged
            payload = {
                "schema_version": 1,
                "days": {key: asdict(merged[key]) for key in sorted(merged)},
            }
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return merged


def _period_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _period_values(records: dict[str, DailyEnergy], start: date, end: date) -> list[DailyEnergy]:
    return [records[day.isoformat()] for day in _period_days(start, end) if day.isoformat() in records]


def prior_mean(records: dict[str, DailyEnergy], report_day: date) -> tuple[float | None, int]:
    """Mean of D-7..D-1; needs three measured days so one outlier is not a baseline."""
    values = _period_values(records, report_day - timedelta(days=7), report_day - timedelta(days=1))
    if len(values) < 3:
        return None, len(values)
    return sum(row.kwh for row in values) / len(values), len(values)


def energy_series(
    records: dict[str, DailyEnergy], report_day: date, days: int = 8,
) -> list[tuple[date, float | None]]:
    start = report_day - timedelta(days=days - 1)
    return [
        (day, records[day.isoformat()].kwh if day.isoformat() in records else None)
        for day in _period_days(start, report_day)
    ]


def period_summaries(records: dict[str, DailyEnergy], report_day: date) -> list[str]:
    """Calendar summaries due on D+1 (Monday: last week; 1st: last month), without details."""
    briefing_day = report_day + timedelta(days=1)
    lines: list[str] = []
    if briefing_day.weekday() == 0:
        start = report_day - timedelta(days=6)
        lines.extend(_period_lines(
            records, start, report_day, label="上周",
            previous_start=start - timedelta(days=7), previous_end=start - timedelta(days=1),
        ))
    if briefing_day.day == 1:
        start = report_day.replace(day=1)
        previous_end = start - timedelta(days=1)
        lines.extend(_period_lines(
            records, start, report_day, label="上月",
            previous_start=previous_end.replace(day=1), previous_end=previous_end,
        ))
    return [line.removeprefix("- ") for line in lines]


def _delta(total: float, baseline: float, label: str) -> str:
    change = total - baseline
    text = f"；较{label} {change:+.2f} kWh"
    if baseline > 0:
        text += f"（{change / baseline * 100:+.1f}%）"
    return text


def _period_lines(
    records: dict[str, DailyEnergy], start: date, end: date, *,
    label: str, previous_start: date, previous_end: date, details: bool = False,
) -> list[str]:
    days = _period_days(start, end)
    values = _period_values(records, start, end)
    complete = len(values) == len(days)
    coverage = f"完整{label[-1]}" if complete else f"数据 {len(values)}/{len(days)} 天"
    lines = [f"- {label}（{start}–{end}，{coverage}）："]
    if values:
        total = sum(record.kwh for record in values)
        highest = max(values, key=lambda record: record.kwh)
        lowest = min(values, key=lambda record: record.kwh)
        average_label = "日均" if complete else "有效日均"
        lines[-1] += (
            f"总量 {total:.2f} kWh，{average_label} {total / len(values):.2f} kWh；"
            f"最高 {highest.date} {highest.kwh:.2f} kWh，"
            f"最低 {lowest.date} {lowest.kwh:.2f} kWh"
        )
        previous = _period_values(records, previous_start, previous_end)
        if complete and len(previous) == len(_period_days(previous_start, previous_end)):
            lines[-1] += _delta(total, sum(row.kwh for row in previous), f"前{label[-1]}")
    else:
        lines[-1] += "用电数据暂缺"
    if details:
        lines.append("- 上月每日明细：")
        for day in days:
            row = records.get(day.isoformat())
            value = f"{row.kwh:.2f} kWh" if row else "暂缺"
            lines.append(f"  - {day}：{value}")
    return lines


def format_energy_briefing(report_day: date, records: dict[str, DailyEnergy]) -> str:
    """Report D's closing and add calendar summaries on D+1's Monday/month start."""
    lines = ["### ⚡ 小米插座用电"]
    current = records.get(report_day.isoformat())
    if current:
        line = f"- 昨日用电（{report_day}）：{current.kwh:.2f} kWh"
        mean, days = prior_mean(records, report_day)
        if mean is not None:
            line += (
                f"；前 7 日均值 {mean:.2f} kWh（{days}/7 天），"
                f"较均值 {current.kwh - mean:+.2f} kWh"
            )
        lines.append(line)
    else:
        lines.append(f"- 昨日用电（{report_day}）：用电数据暂缺")
    briefing_day = report_day + timedelta(days=1)
    if briefing_day.weekday() == 0:
        start = report_day - timedelta(days=6)
        lines.extend(_period_lines(
            records, start, report_day, label="上周",
            previous_start=start - timedelta(days=7), previous_end=start - timedelta(days=1),
        ))
    if briefing_day.day == 1:
        start = report_day.replace(day=1)
        previous_end = start - timedelta(days=1)
        lines.extend(_period_lines(
            records, start, report_day, label="上月",
            previous_start=previous_end.replace(day=1), previous_end=previous_end, details=True,
        ))
    return "\n".join(lines)


def refresh_energy_records(
    report_day: date, cfg: EnergyUsage, *,
    source: EnergySource | None = None, ledger: EnergyLedger | None = None,
) -> dict[str, DailyEnergy]:
    """Read recent closings from HA, merge them into the ledger and return this entity's days."""
    source = source if source is not None else SSHEnergySource(cfg)
    ledger = ledger if ledger is not None else EnergyLedger(cfg.ledger_path)
    previous_month_end = report_day.replace(day=1) - timedelta(days=1)
    start_day = min(previous_month_end.replace(day=1), report_day - timedelta(days=13))
    readings = source.read(
        cfg.entity_id, datetime.combine(start_day, time.min, BEIJING),
        datetime.combine(report_day + timedelta(days=1), time(0, 10), BEIJING),
    )
    closings = daily_closings(readings, start_day, report_day, cfg.entity_id)
    existing = ledger.read()
    try:
        records = ledger.update(closings)
    except Exception as exc:
        log.warning("energy ledger write skipped (non-fatal): %s", exc)
        records = dict(existing)
        for record in closings:
            previous = records.get(record.date)
            if previous is None or record.last_updated >= previous.last_updated:
                records[record.date] = record
    return {key: row for key, row in records.items() if row.entity_id == cfg.entity_id}


def read_energy_records(cfg: EnergyUsage) -> dict[str, DailyEnergy]:
    """Ledger only; used where Home Assistant must not be queried again."""
    records = EnergyLedger(cfg.ledger_path).read()
    return {key: row for key, row in records.items() if row.entity_id == cfg.entity_id}


def build_energy_briefing(
    report_day: date, cfg: EnergyUsage, *,
    source: EnergySource | None = None, ledger: EnergyLedger | None = None,
) -> str:
    if not cfg.enabled:
        return ""
    try:
        records = refresh_energy_records(report_day, cfg, source=source, ledger=ledger)
        return format_energy_briefing(report_day, records)
    except Exception as exc:
        log.warning("energy briefing unavailable (non-fatal): %s", exc)
        return UNAVAILABLE
