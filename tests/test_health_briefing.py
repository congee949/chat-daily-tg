from datetime import date, datetime, timedelta, timezone

from chat_daily_tg.config import HealthBriefing
from chat_daily_tg.health_briefing import (
    APPLE_EPOCH,
    ActivityDay,
    HealthExportReader,
    SleepEpisode,
    _progress,
    build_health_briefing,
    build_health_report,
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


def test_report_falls_back_to_previous_complete_sleep_when_morning_is_missing(monkeypatch):
    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    previous = SleepEpisode(
        datetime(2026, 7, 14, 23, 58, tzinfo=tz),
        datetime(2026, 7, 15, 7, 12, tzinfo=tz),
        7.0, 0.2, 4.4, 0.7, 1.9,
    )

    class FakeReader:
        def __init__(self, *args):
            self.tz = tz

        def activity_day(self, day):
            return ActivityDay(None, 46, 5, None, 3.35, None, None)

        def sleep_ending(self, day):
            return previous if day == date(2026, 7, 15) else None

        def workouts(self, day):
            return []

    monkeypatch.setattr("chat_daily_tg.health_briefing.HealthExportReader", FakeReader)
    report = build_health_report(
        date(2026, 7, 15),
        HealthBriefing(enabled=True, baseline_days=7, min_baseline_samples=7),
        "Asia/Shanghai",
    )
    assert report is not None
    assert report.wake_sleep is None
    assert report.sleep == previous
    assert report.sleep_label == "最近完整睡眠（截至昨日早晨）"


def test_health_card_and_rich_markdown_include_visual_and_native_details(
    monkeypatch, tmp_path
):
    from chat_daily_tg.health_card import render_health_card
    from chat_daily_tg.health_rich import build_health_rich_markdown

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    sleep = SleepEpisode(
        datetime(2026, 7, 14, 23, 58, tzinfo=tz),
        datetime(2026, 7, 15, 7, 12, tzinfo=tz),
        7.0, 0.2, 4.4, 0.7, 1.9,
    )

    class FakeReader:
        def __init__(self, *args):
            self.tz = tz

        def activity_day(self, day):
            return ActivityDay(500, 46, 5, 8000, 3.35, 63, 43)

        def sleep_ending(self, day):
            return sleep

        def workouts(self, day):
            return [{
                "name": "Core Training",
                "duration": 1200,
                "activeEnergy": 418.4,
            }]

    monkeypatch.setattr("chat_daily_tg.health_briefing.HealthExportReader", FakeReader)
    report = build_health_report(
        date(2026, 7, 15),
        HealthBriefing(enabled=True, baseline_days=7, min_baseline_samples=7),
        "Asia/Shanghai",
    )
    assert report is not None
    output = render_health_card(report, tmp_path / "health.png")
    assert output is not None and output.stat().st_size > 1000

    rich = build_health_rich_markdown(report, chart_media_id="health_chart")
    assert "tg://photo?id=health_chart" in rich
    assert "昨日睡眠与训练概览" not in rich
    assert "昨日锻炼" in rich and "近期日常" in rich
    assert "<details><summary>" in rich
    assert "| 项目 | 数据 |" in rich
    assert "| 项目 | 消耗能量 |" in rich
    assert "| 指标 | 差值 |" in rich
    assert "| Core Training | 100千卡 |" in rich
    assert "| 时段 |" not in rich
    assert "| 昨日 | 近期值 |" not in rich
    assert "缺失值不按 0 处理" not in rich
    assert "至少 7 个有效日" not in rich
    assert "Core Training" in rich
    assert "暂不比较" not in rich

    import dataclasses

    cold = dataclasses.replace(
        report,
        medians={},
        baseline_samples={"sleep": 3, "exercise": 3, "distance": 3, "stand": 3},
    )
    cold_rich = build_health_rich_markdown(cold, chart_media_id=None)
    assert "近期基线样本不足（3 天，需 7 天）" in cold_rich
    assert "| 睡眠 | — |" in cold_rich


def test_health_card_relative_symbols():
    from chat_daily_tg.health_card import _relative_symbol

    assert _relative_symbol(1.2)[0] == "↑"
    assert _relative_symbol(1.0)[0] == "="
    assert _relative_symbol(0.8)[0] == "↓"
    assert _relative_symbol(None)[0] == "–"


def test_health_rich_signed_deltas():
    from chat_daily_tg.health_rich import _signed_delta, _sleep_delta

    assert _signed_delta(46, 7, "分钟") == "+39分钟"
    assert _signed_delta(5, 6, "小时") == "-1小时"
    assert _signed_delta(3.35, 1.89, "公里", digits=2) == "+1.46公里"
    assert _sleep_delta(6.333, 7.067) == "-44分钟"
    # exact-half deltas must not leak the formatter's signed zero ("+0"/"-0")
    assert _signed_delta(31.0, 30.5, "分钟") == "0分钟"
    assert _signed_delta(10.0, 10.5, "小时") == "0小时"
    assert _signed_delta(1.5, 1.5, "公里", digits=2) == "0.00公里"
    assert _sleep_delta(7.0, 7.00833) == "0分钟"
    assert _signed_delta(None, 5, "分钟") == "—"
    assert _signed_delta(5, None, "分钟") == "—"
    assert _sleep_delta(None, 7.0) == "—"


# ---- wait_for_wake_signal ---------------------------------------------------

def _wake_stub_reader(results):
    """Factory whose successive INSTANCES pop `results` for sleep_ending()."""
    queue = list(results)

    class Stub:
        def __init__(self, *args, **kwargs):
            pass

        def sleep_ending(self, wake_day):
            return queue.pop(0) if queue else None

    return Stub


def _episode_ending(when):
    from types import SimpleNamespace
    return SimpleNamespace(end=when)


def test_wait_for_wake_disabled_never_polls(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    def boom(*args, **kwargs):
        raise AssertionError("must not construct a reader when disabled")

    monkeypatch.setattr(hb, "HealthExportReader", boom)
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=False), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


def test_wait_for_wake_signal_present_returns_without_sleeping(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    episode = _episode_ending(datetime(2026, 7, 17, 8, 40, tzinfo=tz))
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([episode]))
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is True


def test_wait_for_wake_past_deadline_probes_once_no_wait(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([None]))
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: datetime(2026, 7, 17, 15, 0, tzinfo=tz))
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


def test_wait_for_wake_polls_until_sync_lands(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    episode = _episode_ending(datetime(2026, 7, 17, 9, 12, tzinfo=tz))
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([None, None, episode]))
    nows = iter([
        datetime(2026, 7, 17, 7, 5, tzinfo=tz),
        datetime(2026, 7, 17, 7, 10, tzinfo=tz),
    ])
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: next(nows))
    slept: list[float] = []
    monkeypatch.setattr(hb, "_sleep", slept.append)
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is True
    assert slept == [300, 300]


def test_wait_for_wake_deadline_fallback_clamps_last_sleep(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([]))  # never syncs
    nows = iter([
        datetime(2026, 7, 17, 12, 56, tzinfo=tz),
        datetime(2026, 7, 17, 13, 1, tzinfo=tz),
    ])
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: next(nows))
    slept: list[float] = []
    monkeypatch.setattr(hb, "_sleep", slept.append)
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False
    assert slept == [240]  # min(poll_seconds, seconds till 13:00)


def test_wait_for_wake_reader_crash_is_nonfatal(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz

    def crash(*args, **kwargs):
        raise OSError("icloud hiccup")

    monkeypatch.setattr(hb, "HealthExportReader", crash)
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: datetime(2026, 7, 17, 14, 0, tzinfo=tz))
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


def test_wait_for_wake_ignores_yesterdays_evening_nap(monkeypatch):
    """A >=2h nap that synced last night ends BEFORE wake_day — it must not
    open the gate; the overnight episode syncing later must."""
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    nap = _episode_ending(datetime(2026, 7, 16, 21, 30, tzinfo=tz))
    overnight = _episode_ending(datetime(2026, 7, 17, 9, 5, tzinfo=tz))
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([nap, overnight]))
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: datetime(2026, 7, 17, 7, 5, tzinfo=tz))
    slept: list[float] = []
    monkeypatch.setattr(hb, "_sleep", slept.append)
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is True
    assert slept == [300]  # nap rejected → one real wait → overnight accepted
