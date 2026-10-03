"""Deterministic Apple Watch / Health Auto Export preface for the daily digest."""
from __future__ import annotations

import calendar
from collections import OrderedDict
import json
import logging
import math
import os
import shutil
import statistics
import subprocess
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from chat_daily_tg.config import HealthBriefing

log = logging.getLogger(__name__)

# Module-level seams so tests can drive the wait loop without real sleeping.
_sleep = _time.sleep


def _wake_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
SLEEP_KEYS = ("core", "deep", "rem")
SLEEP_PENDING_MESSAGE = "昨夜睡眠尚未同步，稍后补发"


@dataclass(frozen=True)
class SleepEpisode:
    start: datetime
    end: datetime
    asleep_hours: float
    awake_hours: float
    core_hours: float
    deep_hours: float
    rem_hours: float


@dataclass(frozen=True)
class ActivityDay:
    active_kcal: float | None
    exercise_min: float | None
    stand_hours: float | None
    steps: float | None
    distance_km: float | None
    resting_hr: float | None
    hrv_ms: float | None


@dataclass(frozen=True)
class WorkoutSummary:
    name: str
    start: datetime | None
    end: datetime | None
    duration_min: float
    active_kcal: float | None
    distance_km: float | None


@dataclass(frozen=True)
class HealthDataGap:
    """Auditable source-data problem; paths stay relative to the export root."""

    category: str
    metric: str
    source_day: str
    path: str
    detail: str
    transient: bool


@dataclass(frozen=True)
class HealthReport:
    report_day: date
    briefing_day: date
    activity: ActivityDay
    sleep: SleepEpisode | None
    sleep_label: str
    wake_sleep: SleepEpisode | None
    workouts: tuple[WorkoutSummary, ...]
    medians: dict[str, float | None]
    baseline_samples: dict[str, int]
    baseline_days: int
    min_baseline_samples: int
    data_gaps: tuple[HealthDataGap, ...] = ()

    @property
    def last_night_sleep(self) -> SleepEpisode | None:
        episode = self.wake_sleep
        return episode if episode and episode.end.date() == self.briefing_day else None


def _apple_datetime(value: object, tz: ZoneInfo) -> datetime | None:
    try:
        return (APPLE_EPOCH + timedelta(seconds=float(value))).astimezone(tz)
    except (TypeError, ValueError, OverflowError):
        return None


class HealthExportReader:
    """Read Health Auto Export's per-day LZFSE `.hae` files with a run cache."""

    def __init__(self, root: str | Path, timezone_name: str) -> None:
        self.root = Path(root).expanduser()
        self.tz = ZoneInfo(timezone_name)
        # Adjacent local days reuse adjacent UTC chunks. A small LRU keeps that
        # benefit without retaining a whole month of high-frequency samples.
        self._cache: OrderedDict[Path, list[dict] | None] = OrderedDict()
        self._data_gaps: OrderedDict[tuple[str, str], HealthDataGap] = OrderedDict()
        self._legacy_wrappers: set[str] = set()

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return path.name

    def _record_gap(self, path: Path, category: str, detail: str, *, transient: bool) -> None:
        relative = self._relative_path(path)
        parts = Path(relative).parts
        metric = parts[-2] if len(parts) >= 2 and parts[0] == "HealthMetrics" else (
            "workout" if parts and parts[0] == "Workouts" else "unknown"
        )
        source_day = path.stem if path.stem.isdigit() else ""
        gap = HealthDataGap(category, metric, source_day, relative, detail[:200], transient)
        self._data_gaps[(category, relative)] = gap

    @property
    def data_gaps(self) -> tuple[HealthDataGap, ...]:
        return tuple(self._data_gaps.values())

    def log_diagnostic_summary(self) -> None:
        counts: dict[str, int] = {}
        for gap in self._data_gaps.values():
            counts[gap.category] = counts.get(gap.category, 0) + 1
        if counts:
            summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
            log.warning("health export data gaps: %s", summary)
        if self._legacy_wrappers:
            log.info("health export decoded %d legacy HAE1 file(s)", len(self._legacy_wrappers))

    def _ensure_materialized(self, path: Path) -> bool:
        """Pull a dataless iCloud placeholder local before the lock-holding decoder touches it.

        Health Auto Export writes `.hae` into iCloud Drive. While a chunk is still a
        dataless placeholder (metadata present, ``st_blocks == 0``), ``compression_tool``'s
        read triggers a *synchronous* iCloud materialization while it holds a file lock;
        the kernel refuses the cycle as EDEADLK ("Resource deadlock avoided"), the decode
        fails, and the wake-gate mistakes an un-downloaded file for "sleep not synced yet"
        and spins until the 13:00 deadline. A plain, lock-free read here forces the
        download first, so the decode sees a local file. Best-effort: any failure just
        falls through to the normal decode path (no regression versus not calling this).
        """
        try:
            st = os.stat(path)
            if st.st_size > 0 and st.st_blocks == 0:
                with open(path, "rb") as fh:
                    fh.read()
                refreshed = os.stat(path)
                if refreshed.st_blocks == 0:
                    self._record_gap(
                        path, "icloud_placeholder", "file remains dataless after materialization",
                        transient=True,
                    )
                    return False
        except OSError as exc:
            self._record_gap(path, "temporary_read_error", str(exc), transient=True)
            return False
        return True

    def _decode(self, path: Path) -> list[dict] | None:
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        try:
            path.stat()
        except FileNotFoundError:
            self._record_gap(path, "source_missing", "export file does not exist", transient=False)
            self._cache[path] = None
            return None
        except OSError as exc:
            self._record_gap(path, "temporary_read_error", str(exc), transient=True)
            self._cache[path] = None
            return None
        if not self._ensure_materialized(path):
            self._cache[path] = None
            return None
        tool = shutil.which("compression_tool")
        if not tool:
            self._record_gap(
                path, "decoder_unavailable", "compression_tool not found", transient=False,
            )
            self._cache[path] = None
            return None
        try:
            with open(path, "rb") as fh:
                header = fh.read(8)
                legacy_payload = fh.read() if header.startswith(b"HAE1") else None
            if header.startswith(b"HAE1"):
                # Health Auto Export's older container prepends an eight-byte
                # HAE1 header to an otherwise ordinary LZFSE stream.
                proc = subprocess.run(
                    [tool, "-decode", "-i", "/dev/stdin"], input=legacy_payload,
                    capture_output=True, timeout=30, check=False,
                )
                self._legacy_wrappers.add(self._relative_path(path))
            else:
                proc = subprocess.run(
                    [tool, "-decode", "-i", str(path)],
                    capture_output=True, timeout=30, check=False,
                )
            if proc.returncode != 0:
                raise ValueError(proc.stderr.decode("utf-8", "replace")[:200])
            payload = json.loads(proc.stdout)
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                raise ValueError("data is not a list")
            result = [row for row in rows if isinstance(row, dict)]
            if not result:
                self._record_gap(path, "source_empty", "decoded export contains no records",
                                 transient=False)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
            lowered = detail.lower()
            transient = isinstance(exc, (OSError, subprocess.TimeoutExpired)) or any(
                text in lowered for text in ("deadlock", "temporar", "timed out", "timeout")
            )
            self._record_gap(
                path,
                "temporary_read_error" if transient else "decode_invalid",
                detail,
                transient=transient,
            )
            result = None
        self._cache[path] = result
        self._cache.move_to_end(path)
        while len(self._cache) > 48:
            self._cache.popitem(last=False)
        return result

    def metric_records(self, metric: str, local_start: datetime, local_end: datetime) -> tuple[list[dict], bool]:
        # A local calendar window can straddle two UTC-named export files. Include
        # one day of padding on both sides, then filter by record timestamps.
        rows: list[dict] = []
        found = False
        cursor = local_start.date() - timedelta(days=1)
        last = local_end.date() + timedelta(days=1)
        while cursor <= last:
            path = self.root / "HealthMetrics" / metric / f"{cursor:%Y%m%d}.hae"
            decoded = self._decode(path)
            if decoded is not None:
                found = True
                # Files are named by UTC day. A chunk is complete only after that
                # UTC day has ended; otherwise AutoSync may contain a plausible but
                # partial total (the dangerous case for rings/steps).
                utc_day_end = datetime.combine(cursor + timedelta(days=1), time.min, timezone.utc)
                try:
                    complete = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= utc_day_end
                except OSError:
                    complete = False
                if not complete:
                    self._record_gap(
                        path, "source_incomplete", "file was last modified before UTC day end",
                        transient=False,
                    )
                rows.extend({**row, "_source_complete": complete} for row in decoded)
            cursor += timedelta(days=1)

        unique: dict[tuple, dict] = {}
        for row in rows:
            start = _apple_datetime(row.get("start"), self.tz)
            end = _apple_datetime(row.get("end", row.get("start")), self.tz)
            if start is None or end is None or end <= local_start or start >= local_end:
                continue
            stage = next((key for key in (*SLEEP_KEYS, "awake") if key in row), "")
            key = (row.get("start"), row.get("end"), row.get("unit"), row.get("qty"), stage)
            unique[key] = row
        return list(unique.values()), found

    def activity_day(self, day: date) -> ActivityDay:
        start = datetime.combine(day, time.min, self.tz)
        end = start + timedelta(days=1)

        def values(metric: str, unit: str) -> tuple[list[float], bool]:
            rows, found = self.metric_records(metric, start, end)
            out: list[float] = []
            complete = True
            for row in rows:
                if row.get("unit") != unit:
                    continue
                try:
                    out.append(float(row["qty"]))
                    complete = complete and bool(row.get("_source_complete"))
                except (KeyError, TypeError, ValueError):
                    continue
            return out, bool(out) and complete

        active, active_found = values("active_energy", "kcal")
        exercise, exercise_found = values("apple_exercise_time", "min")
        stand, stand_found = values("apple_stand_hour", "count")
        steps, steps_found = values("step_count", "count")
        distance, distance_found = values("walking_running_distance", "km")
        resting, _resting_found = values("resting_heart_rate", "count/min")
        hrv, _hrv_found = values("heart_rate_variability", "ms")

        def total(vals: list[float], complete: bool) -> float | None:
            return sum(vals) if vals and complete else None

        return ActivityDay(
            active_kcal=total(active, active_found),
            exercise_min=total(exercise, exercise_found),
            stand_hours=total(stand, stand_found),
            steps=total(steps, steps_found),
            distance_km=total(distance, distance_found),
            # Point-in-time recovery metrics are unknown when no sample exists;
            # unlike rings/steps, an empty export must not be presented as zero.
            resting_hr=statistics.fmean(resting) if resting and _resting_found else None,
            hrv_ms=statistics.fmean(hrv) if hrv and _hrv_found else None,
        )

    def sleep_ending(self, wake_day: date) -> SleepEpisode | None:
        window_start = datetime.combine(wake_day - timedelta(days=1), time(18), self.tz)
        window_end = datetime.combine(wake_day, time(14), self.tz)
        rows, _found = self.metric_records("sleep_analysis", window_start, window_end)
        intervals: list[tuple[datetime, datetime, dict]] = []
        for row in rows:
            start = _apple_datetime(row.get("start"), self.tz)
            end = _apple_datetime(row.get("end"), self.tz)
            if start and end and end > start:
                intervals.append((max(start, window_start), min(end, window_end), row))
        if not intervals:
            return None

        # Separate a main overnight episode from evening naps using a 90-minute gap.
        clusters: list[list[tuple[datetime, datetime, dict]]] = []
        for item in sorted(intervals, key=lambda x: (x[0], x[1])):
            if not clusters or item[0] - max(x[1] for x in clusters[-1]) > timedelta(minutes=90):
                clusters.append([item])
            else:
                clusters[-1].append(item)

        candidates: list[SleepEpisode] = []
        for cluster in clusters:
            stage_hours = {"core": 0.0, "deep": 0.0, "rem": 0.0, "awake": 0.0}
            for start, end, row in cluster:
                hours = (end - start).total_seconds() / 3600
                stage = next((key for key in stage_hours if key in row), None)
                if stage:
                    stage_hours[stage] += hours
            asleep = sum(stage_hours[key] for key in SLEEP_KEYS)
            if asleep < 2:
                continue
            candidates.append(SleepEpisode(
                start=min(x[0] for x in cluster),
                end=max(x[1] for x in cluster),
                asleep_hours=asleep,
                awake_hours=stage_hours["awake"],
                core_hours=stage_hours["core"],
                deep_hours=stage_hours["deep"],
                rem_hours=stage_hours["rem"],
            ))
        return max(candidates, key=lambda x: x.asleep_hours, default=None)

    def workouts(self, day: date) -> list[dict]:
        workout_dir = self.root / "Workouts"
        if not workout_dir.is_dir():
            return []
        out: list[dict] = []
        for path in workout_dir.glob(f"*_{day:%Y%m%d}_*.hae"):
            decoded = self._decode_object(path)
            if decoded:
                out.append(decoded)
        return sorted(out, key=lambda row: float(row.get("start", 0)))

    def _decode_object(self, path: Path) -> dict | None:
        # Workout files contain one object rather than a {data:[...]} metric payload.
        tool = shutil.which("compression_tool")
        if not tool:
            return None
        self._ensure_materialized(path)
        try:
            proc = subprocess.run([tool, "-decode", "-i", str(path)], capture_output=True,
                                  timeout=30, check=False)
            obj = json.loads(proc.stdout) if proc.returncode == 0 else None
            return obj if isinstance(obj, dict) else None
        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
            return None


def _median(values: list[float], minimum: int) -> float | None:
    clean = [v for v in values if v is not None and math.isfinite(v)]
    return statistics.median(clean) if len(clean) >= minimum else None


def _pct_delta(value: float | None, baseline: float | None) -> str:
    if value is None or baseline is None or baseline == 0:
        return ""
    pct = (value / baseline - 1) * 100
    return f"（较基线 {'+' if pct >= 0 else ''}{pct:.0f}%）"


def _clock_delta(current: datetime, baseline_minutes: float | None) -> str:
    if baseline_minutes is None:
        return ""
    current_minutes = current.hour * 60 + current.minute
    delta = int(round(current_minutes - baseline_minutes))
    if abs(delta) < 15:
        return "，与基线基本一致"
    return f"，较基线{'晚' if delta > 0 else '早'} {abs(delta)} 分钟"


def _progress(day: date) -> tuple[str, str]:
    days = 366 if calendar.isleap(day.year) else 365
    elapsed = day.timetuple().tm_yday
    pct = elapsed / days * 100
    filled = min(20, max(0, int(pct / 5)))
    return f"第 {elapsed}/{days} 天 · {pct:.1f}%", "█" * filled + "░" * (20 - filled)


def _fmt_workouts(workouts: list[dict]) -> str:
    if not workouts:
        return "未记录 Apple Watch 体能训练"
    parts: list[str] = []
    for workout in workouts[:4]:
        name = str(workout.get("name") or "Workout")
        duration = float(workout.get("duration") or 0) / 60
        segment = f"{name} {duration:.0f} 分钟"
        energy_kj = float(workout.get("activeEnergy") or 0)
        if energy_kj > 0:
            segment += f" / {energy_kj / 4.184:.0f} kcal"
        distance = float(workout.get("totalDistance") or 0)
        if distance > 0:
            segment += f" / {distance:.2f} km"
        parts.append(segment)
    return "；".join(parts)


def _summarize_workouts(reader: HealthExportReader, day: date) -> tuple[WorkoutSummary, ...]:
    out: list[WorkoutSummary] = []
    tz = getattr(reader, "tz", timezone.utc)
    for workout in reader.workouts(day):
        try:
            duration = float(workout.get("duration") or 0) / 60
        except (TypeError, ValueError):
            duration = 0
        try:
            energy_kj = float(workout.get("activeEnergy") or 0)
        except (TypeError, ValueError):
            energy_kj = 0
        try:
            distance = float(workout.get("totalDistance") or 0)
        except (TypeError, ValueError):
            distance = 0
        out.append(WorkoutSummary(
            name=str(workout.get("name") or "Workout"),
            start=_apple_datetime(workout.get("start"), tz),
            end=_apple_datetime(workout.get("end"), tz),
            duration_min=duration,
            active_kcal=energy_kj / 4.184 if energy_kj > 0 else None,
            distance_km=distance if distance > 0 else None,
        ))
    return tuple(out)


def _fmt_workout_summaries(workouts: tuple[WorkoutSummary, ...]) -> str:
    if not workouts:
        return "未记录 Apple Watch 体能训练"
    parts: list[str] = []
    for workout in workouts[:4]:
        segment = f"{workout.name} {workout.duration_min:.0f} 分钟"
        if workout.active_kcal is not None:
            segment += f" / {workout.active_kcal:.0f} kcal"
        if workout.distance_km is not None:
            segment += f" / {workout.distance_km:.2f} km"
        parts.append(segment)
    return "；".join(parts)


def _activity_assessment(activity: ActivityDay, medians: dict[str, float | None]) -> str:
    ratios: list[float] = []
    for value, key in ((activity.active_kcal, "active"),
                       (activity.exercise_min, "exercise"), (activity.steps, "steps")):
        baseline = medians.get(key)
        if value is not None and baseline and baseline > 0:
            ratios.append(value / baseline)
    if not ratios:
        return ""
    score = statistics.median(ratios)
    if score >= 1.2:
        return "整体活动负荷明显高于近期常态"
    if score <= 0.7:
        return "整体活动负荷明显低于近期常态"
    return "整体活动负荷接近近期常态"


def _recovery_assessment(activity: ActivityDay, sleep: SleepEpisode | None,
                         medians: dict[str, float | None]) -> str:
    signals: list[int] = []
    if sleep and medians.get("sleep"):
        signals.append(1 if sleep.asleep_hours >= medians["sleep"] * 0.95 else -1)
    if activity.hrv_ms and medians.get("hrv"):
        signals.append(1 if activity.hrv_ms >= medians["hrv"] * 0.95 else -1)
    if activity.resting_hr and medians.get("rhr"):
        signals.append(1 if activity.resting_hr <= medians["rhr"] * 1.05 else -1)
    if len(signals) < 2:
        return ""
    score = sum(signals)
    if score >= 2:
        return "睡眠与心血管指标整体支持正常恢复" if sleep else "心血管指标整体支持正常恢复"
    if score <= -2:
        return "多项恢复指标低于个人常态，今日宜控制训练负荷"
    return "恢复信号有分歧，结合主观疲劳再决定训练强度"


def wait_for_wake_signal(
    cfg: HealthBriefing,
    wake_day: date,
    timezone_name: str,
    deadline: str = "13:00",
    poll_seconds: float = 300,
) -> bool:
    """Probe once for wake_day's overnight sleep; never block the digest on missing data.

    Rule (2026-07-21): sleep is optional enrichment, not a gate.
    - Episode already synced → return True (health card can use real wake time).
    - No usable sleep data (unsynced / unreadable / only a pre-wake nap) → return
      False immediately and let the caller deliver the group summary now.
      Previously this spun until ``deadline`` (default 13:00); that delayed the
      digest when Health Auto Export lagged or iCloud decode failed.

    ``deadline`` / ``poll_seconds`` are kept for call-site compatibility but are
    no longer used to wait — a single probe always decides.
    """
    _ = (deadline, poll_seconds)  # API/launchd compat; waiting removed by policy
    if not cfg.enabled:
        return False
    log.info("wake-signal probe (no wait if sleep missing): wake_day=%s", wake_day)
    try:
        # Fresh reader — the run cache pins "file missing"; keep the same shape
        # as the old poll loop in case this is ever re-entered within a process.
        episode = HealthExportReader(cfg.export_dir, timezone_name).sleep_ending(wake_day)
    except Exception as exc:
        log.warning("wake-signal probe failed (non-fatal, delivering now): %s", exc)
        return False
    # Only an episode that ENDED on wake_day counts. sleep_ending() returns the
    # LONGEST cluster in its 18:00→14:00 window, so a ≥2h evening nap that
    # synced last night must not count as this morning's wake.
    if (episode and episode.end.astimezone(ZoneInfo(timezone_name)).date() == wake_day
            and episode.end <= _wake_now(ZoneInfo(timezone_name))):
        log.info("wake signal: sleep ended %s", f"{episode.end:%H:%M}")
        return True
    log.info("no sleep data for %s — delivering summary without waiting", wake_day)
    return False


def build_health_report(report_day: date, cfg: HealthBriefing, timezone_name: str) -> HealthReport | None:
    """Read health data once and return a reusable text/chart/rich-message model."""
    if not cfg.enabled:
        return None
    reader = HealthExportReader(cfg.export_dir, timezone_name)
    briefing_day = report_day + timedelta(days=1)
    activity = reader.activity_day(report_day)
    wake_sleep = reader.sleep_ending(briefing_day)
    tz = ZoneInfo(timezone_name)
    if wake_sleep and (
        wake_sleep.end.astimezone(tz).date() != briefing_day
        or wake_sleep.end > _wake_now(tz)
    ):
        wake_sleep = None
    sleep = wake_sleep
    sleep_label = "昨夜睡眠" if wake_sleep else ""

    baselines: dict[str, list[float]] = {
        "active": [], "exercise": [], "stand": [], "steps": [], "distance": [],
        "rhr": [], "hrv": [], "sleep": [], "wake": [],
    }
    for offset in range(cfg.baseline_days, 0, -1):
        day = report_day - timedelta(days=offset)
        prior = reader.activity_day(day)
        for key, value in (
            ("active", prior.active_kcal), ("exercise", prior.exercise_min),
            ("stand", prior.stand_hours), ("steps", prior.steps),
            ("distance", prior.distance_km), ("rhr", prior.resting_hr),
            ("hrv", prior.hrv_ms),
        ):
            if value is not None and value > 0:
                baselines[key].append(value)
        prior_sleep = reader.sleep_ending(day + timedelta(days=1))
        if prior_sleep and prior_sleep.end.astimezone(tz).date() == day + timedelta(days=1):
            baselines["sleep"].append(prior_sleep.asleep_hours)
            baselines["wake"].append(prior_sleep.end.hour * 60 + prior_sleep.end.minute)

    minimum = cfg.min_baseline_samples
    median = {key: _median(values, minimum) for key, values in baselines.items()}
    data_gaps = tuple(getattr(reader, "data_gaps", ()))
    log_summary = getattr(reader, "log_diagnostic_summary", None)
    if callable(log_summary):
        log_summary()
    return HealthReport(
        report_day=report_day,
        briefing_day=briefing_day,
        activity=activity,
        sleep=sleep,
        sleep_label=sleep_label,
        wake_sleep=wake_sleep,
        workouts=_summarize_workouts(reader, report_day),
        medians=median,
        baseline_samples={key: len(values) for key, values in baselines.items()},
        baseline_days=cfg.baseline_days,
        min_baseline_samples=minimum,
        data_gaps=data_gaps,
    )


def write_health_gap_record(report: HealthReport, path: str | Path) -> Path:
    """Atomically persist the Health source-gap audit beside the daily report."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for gap in report.data_gaps:
        counts[gap.category] = counts.get(gap.category, 0) + 1
    payload = {
        "schema_version": 1,
        "report_day": report.report_day.isoformat(),
        "briefing_day": report.briefing_day.isoformat(),
        "status": "degraded" if report.data_gaps else "complete",
        "summary": {key: counts[key] for key in sorted(counts)},
        "gaps": [asdict(gap) for gap in report.data_gaps],
    }
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def format_health_briefing(report: HealthReport) -> str:
    """Format the classic Markdown fallback from a structured health report."""
    activity = report.activity
    sleep = report.last_night_sleep
    median = report.medians
    briefing_day = report.briefing_day
    progress, bar = _progress(briefing_day)
    lines = [f"### 🌤️ 个人晨报 · {briefing_day.isoformat()}"]
    if sleep:
        lines.append(
            f"- 起床：{sleep.end:%H:%M}"
            f"（依据最后睡眠阶段推定{_clock_delta(sleep.end, median['wake'])}）"
        )
    else:
        lines.append(f"- {SLEEP_PENDING_MESSAGE}")
    lines.extend([f"- 年度：{progress}", bar])

    activity_parts: list[str] = []
    if activity.active_kcal is not None:
        activity_parts.append(f"活动能量 {activity.active_kcal:.0f} kcal{_pct_delta(activity.active_kcal, median['active'])}")
    if activity.exercise_min is not None:
        activity_parts.append(f"锻炼 {activity.exercise_min:.0f} 分钟{_pct_delta(activity.exercise_min, median['exercise'])}")
    if activity.stand_hours is not None:
        activity_parts.append(f"站立 {activity.stand_hours:.0f} 小时")
    if activity.steps is not None:
        activity_parts.append(f"{activity.steps:.0f} 步{_pct_delta(activity.steps, median['steps'])}")
    if activity.distance_km is not None:
        activity_parts.append(f"步行/跑步 {activity.distance_km:.2f} km")
    lines.append("- 昨日活动：" + ("；".join(activity_parts) if activity_parts else "健康数据尚未同步"))
    activity_judgment = _activity_assessment(activity, median)
    if activity_judgment:
        lines.append(f"- 活动判断：{activity_judgment}")
    lines.append(f"- 训练：{_fmt_workout_summaries(report.workouts)}")

    if sleep:
        sleep_delta = _pct_delta(sleep.asleep_hours, median["sleep"])
        lines.append(
            f"- 昨夜睡眠：{sleep.start:%H:%M}–{sleep.end:%H:%M}，"
            f"实睡 {sleep.asleep_hours:.1f} 小时{sleep_delta}；"
            f"核心 {sleep.core_hours:.1f}h / 深睡 {sleep.deep_hours:.1f}h / "
            f"REM {sleep.rem_hours:.1f}h / 清醒 {sleep.awake_hours:.1f}h"
        )
    recovery: list[str] = []
    if activity.resting_hr and median["rhr"]:
        recovery.append(f"静息心率 {activity.resting_hr:.0f}（基线 {median['rhr']:.0f}）")
    if activity.hrv_ms and median["hrv"]:
        recovery.append(f"HRV {activity.hrv_ms:.0f} ms（基线 {median['hrv']:.0f}）")
    if recovery:
        lines.append("- 恢复：" + "；".join(recovery))
    recovery_judgment = _recovery_assessment(activity, sleep, median)
    if recovery_judgment:
        lines.append(f"- 恢复判断：{recovery_judgment}")
    lines.append(
        f"- 基线：过去 {report.baseline_days} 天中至少 "
        f"{report.min_baseline_samples} 个有效日的中位数"
    )
    return "\n".join(lines)


def build_health_briefing(report_day: date, cfg: HealthBriefing, timezone_name: str) -> str:
    """Build the classic preface. `report_day` is yesterday."""
    report = build_health_report(report_day, cfg, timezone_name)
    return format_health_briefing(report) if report else ""
