from datetime import date, datetime, timedelta, timezone

from chat_daily_tg.config import HealthBriefing
from chat_daily_tg.health_briefing import (
    APPLE_EPOCH,
    ActivityDay,
    HealthExportReader,
    SleepEpisode,
    _progress,
    build_health_briefing,
)


def _apple_seconds(value: datetime) -> float:
    return (value.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds()


def test_sleep_ending_selects_main_episode_not_evening_nap(tmp_path):
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    tz = reader.tz

    def row(start, end, stage):
        hours = (end - start).total_seconds() / 3600
        return {"start": _apple_seconds(start), "end": _apple_seconds(end), stage: hours,
                "totalSleep": 0 if stage == "awake" else hours, "unit": "hr"}

    rows = [
        row(datetime(2026, 7, 15, 19, 0, tzinfo=tz), datetime(2026, 7, 15, 19, 40, tzinfo=tz), "core"),
        row(datetime(2026, 7, 15, 23, 30, tzinfo=tz), datetime(2026, 7, 16, 2, 0, tzinfo=tz), "core"),
        row(datetime(2026, 7, 16, 2, 0, tzinfo=tz), datetime(2026, 7, 16, 3, 0, tzinfo=tz), "deep"),
        row(datetime(2026, 7, 16, 3, 0, tzinfo=tz), datetime(2026, 7, 16, 6, 20, tzinfo=tz), "rem"),
        row(datetime(2026, 7, 16, 6, 20, tzinfo=tz), datetime(2026, 7, 16, 6, 30, tzinfo=tz), "awake"),
    ]
    reader.metric_records = lambda *args: (rows, True)
    episode = reader.sleep_ending(date(2026, 7, 16))
    assert episode is not None
    assert episode.start.strftime("%H:%M") == "23:30"
    assert episode.end.strftime("%H:%M") == "06:30"
    assert round(episode.asleep_hours, 2) == 6.83


def test_progress_uses_real_year_length():
    text, bar = _progress(date(2026, 7, 16))
    assert text == "第 197/365 天 · 54.0%"
    assert len(bar) == 20
    assert bar.count("█") == 10


def test_activity_rejects_partial_autosync_totals(tmp_path):
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    reader.metric_records = lambda *args: ([{
        "qty": 123, "unit": "kcal", "_source_complete": False,
    }], True)
    activity = reader.activity_day(date(2026, 7, 15))
    assert activity.active_kcal is None


def test_briefing_formats_real_values_and_baseline(monkeypatch):
    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    current_sleep = SleepEpisode(
        datetime(2026, 7, 15, 23, 0, tzinfo=tz),
        datetime(2026, 7, 16, 6, 30, tzinfo=tz),
        7.0, 0.5, 4.5, 1.2, 1.3,
    )

    class FakeReader:
        def __init__(self, *args):
            pass

        def activity_day(self, day):
            if day == date(2026, 7, 15):
                return ActivityDay(500, 35, 10, 8000, 6.2, 60, 48)
            return ActivityDay(400, 30, 9, 7000, 5.4, 62, 42)

        def sleep_ending(self, day):
            if day == date(2026, 7, 16):
                return current_sleep
            return SleepEpisode(
                datetime.combine(day - timedelta(days=1), datetime.min.time(), tz).replace(hour=23),
                datetime.combine(day, datetime.min.time(), tz).replace(hour=6),
                6.5, 0.3, 4.2, 1.1, 1.2,
            )

        def workouts(self, day):
            return [{"name": "Running", "duration": 1800, "activeEnergy": 836.8,
                     "totalDistance": 5.0}]

    monkeypatch.setattr("chat_daily_tg.health_briefing.HealthExportReader", FakeReader)
    out = build_health_briefing(
        date(2026, 7, 15),
        HealthBriefing(enabled=True, baseline_days=7, min_baseline_samples=7),
        "Asia/Shanghai",
    )
    assert "个人晨报 · 2026-07-16" in out
    assert "起床：06:30" in out
    assert "第 197/365 天 · 54.0%" in out
    assert "活动能量 500 kcal（较基线 +25%）" in out
    assert "活动判断：整体活动负荷接近近期常态" in out
    assert "Running 30 分钟 / 200 kcal / 5.00 km" in out
    assert "实睡 7.0 小时（较基线 +8%）" in out
    assert "恢复判断：睡眠与心血管指标整体支持正常恢复" in out
