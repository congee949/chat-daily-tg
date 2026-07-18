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
from dataclasses import dataclass
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

    @staticmethod
    def _ensure_materialized(path: Path) -> None:
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
        except OSError as exc:
            log.debug("health export materialize skipped for %s: %s", path, exc)

    def _decode(self, path: Path) -> list[dict] | None:
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        if not path.is_file():
            self._cache[path] = None
            return None
        self._ensure_materialized(path)
        tool = shutil.which("compression_tool")
        if not tool:
            log.warning("health briefing unavailable: compression_tool not found")
            self._cache[path] = None
            return None
        try:
            proc = subprocess.run(
                [tool, "-decode", "-i", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(proc.stderr.decode("utf-8", "replace")[:200])
            payload = json.loads(proc.stdout)
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                raise ValueError("data is not a list")
            result = [row for row in rows if isinstance(row, dict)]
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            log.warning("health export decode failed for %s: %s", path, exc)
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
        return "睡眠与心血管指标整体支持正常恢复"
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
    """Block until wake_day's overnight sleep episode syncs in, or the deadline passes.

    Gates the morning digest on the user actually being awake: the Watch sleep
    episode reaching iCloud (phone unlocked after waking → Health Auto Export
    syncs) IS the wake signal. Returns True once the episode is readable, False
    when the local-time deadline forces the fallback — the caller delivers
    either way (rather deliver than drop), the briefing just notes the missing
    sync. A wake_day whose deadline already passed (backfill, post-noon Mac
    wake-up) gets exactly one probe and no waiting.
    """
    if not cfg.enabled:
        return False
    tz = ZoneInfo(timezone_name)
    hour, minute = (int(part) for part in deadline.split(":", 1))
    deadline_at = datetime.combine(wake_day, time(hour, minute), tzinfo=tz)
    log.info("waiting for wake signal: poll every %.0fs, deadline %s %s",
             poll_seconds, wake_day, deadline)
    polls = 1
    while True:
        try:
            # Fresh reader per poll — the run cache pins "file missing", so a
            # reused instance would never see the iCloud sync land.
            episode = HealthExportReader(cfg.export_dir, timezone_name).sleep_ending(wake_day)
        except Exception as exc:
            episode = None
            log.warning("wake-signal poll failed (non-fatal): %s", exc)
        # Only an episode that ENDED on wake_day counts. sleep_ending() returns
        # the LONGEST cluster in its 18:00→14:00 window, so a ≥2h evening nap
        # that synced last night would otherwise open the gate at the first poll
        # while the overnight sleep is still unsynced.
        if episode and episode.end.date() >= wake_day:
            log.info("wake signal: sleep ended %s (poll #%d)", f"{episode.end:%H:%M}", polls)
            return True
        remaining = (deadline_at - _wake_now(tz)).total_seconds()
        if remaining <= 0:
            log.info("no wake signal by %s local — delivering without it", deadline)
            return False
        polls += 1
        _sleep(min(poll_seconds, remaining))


def build_health_report(report_day: date, cfg: HealthBriefing, timezone_name: str) -> HealthReport | None:
    """Read health data once and return a reusable text/chart/rich-message model."""
    if not cfg.enabled:
        return None
    reader = HealthExportReader(cfg.export_dir, timezone_name)
    briefing_day = report_day + timedelta(days=1)
    activity = reader.activity_day(report_day)
    wake_sleep = reader.sleep_ending(briefing_day)
    report_sleep = reader.sleep_ending(report_day)
    # AutoSync commonly publishes the previous completed night before the current
    # morning. Keep the briefing useful without pretending stale data is current.
    sleep = wake_sleep or report_sleep
    sleep_label = "昨夜睡眠" if wake_sleep else "最近完整睡眠（截至昨日早晨）"

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
        if prior_sleep:
            baselines["sleep"].append(prior_sleep.asleep_hours)
            baselines["wake"].append(prior_sleep.end.hour * 60 + prior_sleep.end.minute)

    minimum = cfg.min_baseline_samples
    median = {key: _median(values, minimum) for key, values in baselines.items()}
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
    )


def format_health_briefing(report: HealthReport) -> str:
    """Format the classic Markdown fallback from a structured health report."""
    activity = report.activity
    sleep = report.sleep
    median = report.medians
    briefing_day = report.briefing_day
    progress, bar = _progress(briefing_day)
    lines = [f"### 🌤️ 个人晨报 · {briefing_day.isoformat()}"]
    if report.wake_sleep:
        lines.append(
            f"- 起床：{report.wake_sleep.end:%H:%M}"
            f"（依据最后睡眠阶段推定{_clock_delta(report.wake_sleep.end, median['wake'])}）"
        )
    else:
        lines.append("- 起床：今晨睡眠数据尚未同步，暂不判断")
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
            f"- {report.sleep_label}：{sleep.start:%H:%M}–{sleep.end:%H:%M}，"
            f"实睡 {sleep.asleep_hours:.1f} 小时{sleep_delta}；"
            f"深睡 {sleep.deep_hours:.1f}h / REM {sleep.rem_hours:.1f}h / 清醒 {sleep.awake_hours:.1f}h"
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
