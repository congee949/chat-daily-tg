import os
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chat_daily_tg.config import HealthBriefing
from chat_daily_tg.health_briefing import (
    APPLE_EPOCH,
    ActivityDay,
    HealthDataGap,
    HealthExportReader,
    HealthReport,
    SleepEpisode,
    SLEEP_PENDING_MESSAGE,
    _progress,
    build_health_briefing,
    build_health_report,
    format_health_briefing,
    write_health_gap_record,
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


def test_dataless_hae_is_materialized_before_decode(tmp_path, monkeypatch):
    # A dataless iCloud placeholder (st_blocks == 0) must get a lock-free read to
    # pull it local before compression_tool locks it — otherwise the decode hits
    # EDEADLK and the wake-gate spins until its deadline. See health_briefing._decode.
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    path = tmp_path / "20260716.hae"
    path.write_bytes(b"placeholder-bytes")

    real_stat = os.stat

    class _Dataless:
        def __init__(self, st):
            self.st_size = st.st_size
            self.st_blocks = 0  # dataless iCloud placeholder

    def fake_stat(target, *a, **k):
        st = real_stat(target, *a, **k)
        return _Dataless(st) if str(target) == str(path) else st

    reads: list[str] = []
    real_open = open

    def spy_open(target, *a, **k):
        if str(target) == str(path):
            reads.append(str(target))
        return real_open(target, *a, **k)

    monkeypatch.setattr("chat_daily_tg.health_briefing.os.stat", fake_stat, raising=False)
    monkeypatch.setattr("builtins.open", spy_open)
    assert reader._ensure_materialized(path) is False
    assert reads == [str(path)], "dataless placeholder should be read to force materialization"
    assert [gap.category for gap in reader.data_gaps] == ["icloud_placeholder"]


def test_materialized_hae_is_not_reread(tmp_path, monkeypatch):
    # Already-local files (st_blocks > 0) must not be re-read on every decode.
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    path = tmp_path / "20260717.hae"
    path.write_bytes(b"local-bytes")
    reads: list[str] = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda t, *a, **k: (reads.append(str(t)), real_open(t, *a, **k))[1])
    reader._ensure_materialized(path)  # real st_blocks is nonzero for a written file
    assert reads == []


def test_legacy_hae1_wrapper_is_decoded_without_decode_warning(tmp_path, monkeypatch, caplog):
    path = tmp_path / "HealthMetrics" / "active_energy" / "20260727.hae"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"HAE1" + b"\0\0\0\x08" + b"legacy-lfsze-stream")
    decoded = json.dumps({"data": [{"qty": 1, "unit": "kcal"}]}).encode()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=decoded, stderr=b"")

    monkeypatch.setattr("chat_daily_tg.health_briefing.shutil.which", lambda _name: "/tool")
    monkeypatch.setattr("chat_daily_tg.health_briefing.subprocess.run", fake_run)
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    assert reader._decode(path) == [{"qty": 1, "unit": "kcal"}]
    assert calls[0][0][-1] == "/dev/stdin"
    assert calls[0][1]["input"] == b"legacy-lfsze-stream"
    with caplog.at_level("INFO", logger="chat_daily_tg.health_briefing"):
        reader.log_diagnostic_summary()
    assert "decode failed" not in caplog.text
    assert "decoded 1 legacy HAE1 file" in caplog.text


def test_health_decode_failures_are_classified_and_summarized_once(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "HealthMetrics" / "step_count" / "20260729.hae"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not-a-compressed-file")
    monkeypatch.setattr("chat_daily_tg.health_briefing.shutil.which", lambda _name: "/tool")
    monkeypatch.setattr(
        "chat_daily_tg.health_briefing.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"ERROR: could not auto-detect compression type"
        ),
    )
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    assert reader._decode(path) is None
    assert [gap.category for gap in reader.data_gaps] == ["decode_invalid"]
    assert caplog.text == ""
    reader.log_diagnostic_summary()
    assert caplog.text.count("health export data gaps:") == 1
    assert "decode_invalid=1" in caplog.text


def test_temporary_health_read_error_is_not_misreported_as_missing(tmp_path, monkeypatch):
    path = tmp_path / "HealthMetrics" / "sleep_analysis" / "20260808.hae"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"placeholder")
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    real_stat = type(path).stat

    def denied(self, *args, **kwargs):
        if self == path:
            raise OSError("iCloud temporarily unavailable")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "stat", denied)
    assert reader._decode(path) is None
    assert [gap.category for gap in reader.data_gaps] == ["temporary_read_error"]


def test_missing_and_incomplete_health_sources_are_auditable(tmp_path, monkeypatch):
    reader = HealthExportReader(tmp_path, "Asia/Shanghai")
    start = datetime(2026, 7, 30, tzinfo=reader.tz)
    rows, found = reader.metric_records("step_count", start, start + timedelta(days=1))
    assert rows == [] and found is False
    assert {gap.category for gap in reader.data_gaps} == {"source_missing"}

    path = tmp_path / "HealthMetrics" / "active_energy" / "20260730.hae"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"compressed")
    monkeypatch.setattr(reader, "_decode", lambda _path: [{"qty": 1, "unit": "kcal"}])
    reader.metric_records("active_energy", start, start + timedelta(days=1))
    assert "source_incomplete" in {gap.category for gap in reader.data_gaps}


def test_health_gap_record_is_structured_and_atomic(tmp_path):
    gap = HealthDataGap(
        category="source_missing", metric="sleep_analysis", source_day="20260808",
        path="HealthMetrics/sleep_analysis/20260808.hae",
        detail="export file does not exist", transient=False,
    )
    empty_activity = ActivityDay(None, None, None, None, None, None, None)
    report = HealthReport(
        report_day=date(2026, 8, 8), briefing_day=date(2026, 8, 9),
        activity=empty_activity, sleep=None, sleep_label="", wake_sleep=None,
        workouts=(), medians={}, baseline_samples={}, baseline_days=28,
        min_baseline_samples=7, data_gaps=(gap,),
    )
    target = write_health_gap_record(report, tmp_path / "health-data-gaps.json")
    payload = json.loads(target.read_text())
    assert payload["status"] == "degraded"
    assert payload["summary"] == {"source_missing": 1}
    assert payload["gaps"][0]["path"] == "HealthMetrics/sleep_analysis/20260808.hae"
    assert not list(tmp_path.glob("*.tmp"))


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
    assert "核心 4.5h" in out


def test_report_does_not_use_previous_sleep_when_morning_is_missing(monkeypatch, tmp_path):
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
    assert report.sleep is None
    assert report.sleep_label == ""
    assert report.last_night_sleep is None
    plain = format_health_briefing(report)
    assert SLEEP_PENDING_MESSAGE in plain
    assert "07:12" not in plain and "实睡 7.0" not in plain
    assert "锻炼 46 分钟" in plain

    from chat_daily_tg.health_card import _sleep_timeline, render_health_card
    from chat_daily_tg.health_rich import build_health_rich_markdown
    rich = build_health_rich_markdown(report, chart_media_id=None)
    assert SLEEP_PENDING_MESSAGE in rich
    assert "睡眠时段" not in rich and "| 实睡 |" not in rich
    recorded = []
    draw = SimpleNamespace(text=lambda xy, text, **kwargs: recorded.append(text))
    _sleep_timeline(draw, report, 0)
    assert SLEEP_PENDING_MESSAGE in recorded
    assert render_health_card(report, tmp_path / "pending.png") is not None


def test_health_card_and_rich_markdown_include_visual_and_native_details(
    monkeypatch, tmp_path
):
    from chat_daily_tg.health_card import render_health_card
    from chat_daily_tg.health_rich import build_health_rich_markdown

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    sleep = SleepEpisode(
        datetime(2026, 7, 15, 23, 58, tzinfo=tz),
        datetime(2026, 7, 16, 7, 12, tzinfo=tz),
        7.0, 0.2, 4.4, 0.7, 1.9,
    )

    class FakeReader:
        def __init__(self, *args):
            self.tz = tz

        def activity_day(self, day):
            return ActivityDay(500, 46, 5, 8000, 3.35, 63, 43)

        def sleep_ending(self, day):
            if day == date(2026, 7, 16):
                return sleep
            return SleepEpisode(
                datetime.combine(day - timedelta(days=1), datetime.min.time(), tz).replace(hour=23),
                datetime.combine(day, datetime.min.time(), tz).replace(hour=7),
                7.0, 0.2, 4.4, 0.7, 1.9,
            )

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
    assert "| 记录口径 | 昨夜睡眠 |" in rich
    assert "起床：**07:12**" in rich

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


def test_wait_for_wake_missing_sleep_delivers_immediately(monkeypatch):
    """No usable overnight episode → deliver summary now; never spin to deadline."""
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([None]))
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: datetime(2026, 7, 17, 7, 5, tzinfo=tz))
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


def test_wait_for_wake_reader_crash_is_nonfatal(monkeypatch):
    import chat_daily_tg.health_briefing as hb

    def crash(*args, **kwargs):
        raise OSError("icloud hiccup")

    monkeypatch.setattr(hb, "HealthExportReader", crash)
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


def test_wait_for_wake_ignores_yesterdays_evening_nap(monkeypatch):
    """A >=2h nap ending BEFORE wake_day is not today's wake → deliver without waiting."""
    import chat_daily_tg.health_briefing as hb

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    nap = _episode_ending(datetime(2026, 7, 16, 21, 30, tzinfo=tz))
    # Only the nap is available on the single probe; overnight never blocks.
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([nap]))
    monkeypatch.setattr(hb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai"
    ) is False


@pytest.mark.parametrize("end_day", [date(2026, 7, 16), date(2026, 7, 18)])
def test_wake_probe_requires_exact_target_day(monkeypatch, end_day):
    import chat_daily_tg.health_briefing as hb
    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    episode = _episode_ending(datetime.combine(end_day, datetime.min.time(), tz).replace(hour=7))
    monkeypatch.setattr(hb, "HealthExportReader", _wake_stub_reader([episode]))
    assert hb.wait_for_wake_signal(
        HealthBriefing(enabled=True), date(2026, 7, 17), "Asia/Shanghai",
    ) is False


@pytest.mark.parametrize("ending", [
    datetime(2026, 7, 15, 22),
    datetime(2026, 7, 17, 7),
    datetime(2026, 7, 16, 8),
])
def test_health_report_rejects_wrong_day_or_future_end(monkeypatch, ending):
    import chat_daily_tg.health_briefing as hb
    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    end = ending.replace(tzinfo=tz)
    episode = SleepEpisode(end - timedelta(hours=7), end, 7, 0, 5, 1, 1)

    class Reader:
        def __init__(self, *args):
            pass

        def activity_day(self, day):
            return ActivityDay(500, 30, 8, 6000, 4, None, None)

        def sleep_ending(self, day):
            return episode

        def workouts(self, day):
            return []

    monkeypatch.setattr(hb, "HealthExportReader", Reader)
    monkeypatch.setattr(hb, "_wake_now", lambda _tz: datetime(2026, 7, 16, 7, 5, tzinfo=tz))
    report = build_health_report(
        date(2026, 7, 15), HealthBriefing(enabled=True, baseline_days=7),
        "Asia/Shanghai",
    )
    assert report.sleep is None and report.wake_sleep is None
    assert SLEEP_PENDING_MESSAGE in format_health_briefing(report)


def _health_followup_case(monkeypatch, tmp_path, *, synced=True):
    from chat_daily_tg import cli
    import chat_daily_tg.health_briefing as hb
    import chat_daily_tg.health_card as hc
    import chat_daily_tg.tg_sender as ts

    tz = HealthExportReader("/tmp", "Asia/Shanghai").tz
    episode = SleepEpisode(
        datetime(2026, 10, 2, 23, tzinfo=tz),
        datetime(2026, 10, 3, 7, 45, tzinfo=tz),
        7.41, 0.3, 5, 1, 1.41,
    ) if synced else None
    report = HealthReport(
        report_day=date(2026, 10, 2), briefing_day=date(2026, 10, 3),
        activity=ActivityDay(500, 30, 8, 6000, 4, None, None),
        sleep=episode, sleep_label="昨夜睡眠" if synced else "", wake_sleep=episode,
        workouts=(), medians=dict.fromkeys([
            "active", "exercise", "stand", "steps", "distance", "rhr", "hrv", "sleep", "wake",
        ]),
        baseline_samples={}, baseline_days=28, min_baseline_samples=7,
    )
    cfg = SimpleNamespace(
        health_briefing=HealthBriefing(enabled=True),
        schedule=SimpleNamespace(timezone="Asia/Shanghai"),
        telegram=SimpleNamespace(bot_token_env="TEST_HEALTH_TOKEN", chat_id_env="TEST_HEALTH_CHAT"),
        retry=SimpleNamespace(max_attempts=1, backoff_seconds=[]),
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / ".run-complete").touch()
    calls = []
    monkeypatch.setattr(cli.runtime, "prepare_process", lambda: None)
    monkeypatch.setattr(cli, "load_env_file", lambda _path: None)
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "archive_dir_for", lambda _date: archive)
    monkeypatch.setattr(cli.runtime, "resolve_tg_target", lambda *a: ("-100123", 42))
    monkeypatch.setenv("TEST_HEALTH_TOKEN", "unit-test-token")
    monkeypatch.setenv("TEST_HEALTH_CHAT", "unit-test-chat")
    monkeypatch.setattr(hb, "build_health_report", lambda *a: calls.append("read_health") or report)

    def render(_report, path):
        path.write_bytes(b"unit-test-png")
        return path

    monkeypatch.setattr(hc, "render_health_card", render)
    # The followup must stay outside the group-summary pipeline.
    monkeypatch.setattr(
        cli.daily, "run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("daily pipeline called")),
    )

    class Sender:
        def __init__(self, **kwargs):
            calls.append(("sender", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_rich_message(self, **kwargs):
            calls.append(("rich", kwargs))
            return 99

        def send(self, text, **kwargs):
            calls.append(("text", text, kwargs))
            return [99]

    monkeypatch.setattr(ts, "TelegramSender", Sender)
    return cli, archive, calls, Sender


def test_health_followup_delivers_once_and_does_not_rebuild_digest(monkeypatch, tmp_path):
    cli, archive, calls, _sender = _health_followup_case(monkeypatch, tmp_path)
    summary = archive / "summary.md"
    summary.write_text("original group summary", encoding="utf-8")
    args = ["daily", "health-followup", "--date", "2026-10-02"]
    assert cli.main(args) == 0
    assert (archive / ".health-card-sent").exists()
    rich = next(call[1] for call in calls if isinstance(call, tuple) and call[0] == "rich")
    assert "起床：**07:45**" in rich["markdown"]
    assert rich["media"] == [("health_chart", str(archive / "health-card.png"), "photo")]
    before = list(calls)
    assert cli.main(args) == 0
    assert calls == before
    assert summary.read_text() == "original group summary"


def test_health_followup_unsynced_exits_without_delivery_marker(monkeypatch, tmp_path):
    cli, archive, calls, _sender = _health_followup_case(monkeypatch, tmp_path, synced=False)
    assert cli.main(["daily", "health-followup", "--date", "2026-10-02"]) == 0
    assert calls == ["read_health"]
    assert not (archive / ".health-card-sent").exists()
    assert (archive / "health-data-gaps.json").exists()


def test_health_followup_waits_for_main_digest_delivery(monkeypatch, tmp_path):
    cli, archive, calls, _sender = _health_followup_case(monkeypatch, tmp_path)
    (archive / ".run-complete").unlink()
    assert cli.main(["daily", "health-followup", "--date", "2026-10-02"]) == 0
    assert calls == []
    assert not (archive / ".health-card-sent").exists()


def test_health_followup_no_push_only_refreshes_health_artifacts(monkeypatch, tmp_path):
    cli, archive, calls, _sender = _health_followup_case(monkeypatch, tmp_path)
    assert cli.main(["daily", "health-followup", "--date", "2026-10-02", "--no-push"]) == 0
    assert calls == ["read_health"]
    assert (archive / "health-briefing.md").exists()
    assert (archive / "health-rich.md").exists()
    assert not (archive / ".health-card-sent").exists()


def test_health_followup_transport_failure_does_not_mark_success(monkeypatch, tmp_path):
    cli, archive, calls, sender = _health_followup_case(monkeypatch, tmp_path)

    def fail(self, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(sender, "send_rich_message", fail)
    with pytest.raises(OSError, match="offline"):
        cli.main(["daily", "health-followup", "--date", "2026-10-02"])
    assert not (archive / ".health-card-sent").exists()
    assert not any(isinstance(call, tuple) and call[0] == "text" for call in calls)


def test_health_followup_ambiguous_delivery_stops_automatic_replay(monkeypatch, tmp_path):
    import httpx
    from chat_daily_tg.tg_sender import AmbiguousDeliveryError
    cli, archive, calls, sender = _health_followup_case(monkeypatch, tmp_path)

    def ambiguous(self, **kwargs):
        raise AmbiguousDeliveryError("sendRichMessage", httpx.ReadTimeout("response lost"))

    monkeypatch.setattr(sender, "send_rich_message", ambiguous)
    args = ["daily", "health-followup", "--date", "2026-10-02"]
    assert cli.main(args) == 1
    assert (archive / ".health-followup-ambiguous").exists()
    assert not (archive / ".health-card-sent").exists()
    before = list(calls)
    assert cli.main(args) == 1
    assert calls == before


def test_health_followup_rich_rejection_falls_back_to_health_text(monkeypatch, tmp_path):
    import httpx
    cli, archive, calls, sender = _health_followup_case(monkeypatch, tmp_path)

    def rejected(self, **kwargs):
        request = httpx.Request("POST", "https://example.test/sendRichMessage")
        raise httpx.HTTPStatusError(
            "unsupported rich formatting", request=request,
            response=httpx.Response(400, request=request),
        )

    monkeypatch.setattr(sender, "send_rich_message", rejected)
    assert cli.main(["daily", "health-followup", "--date", "2026-10-02"]) == 0
    text = next(call[1] for call in calls if isinstance(call, tuple) and call[0] == "text")
    assert "昨夜睡眠" in text and "实睡 7.4" in text
    assert (archive / ".health-card-sent").exists()


def test_health_followup_overlapping_run_does_not_send(monkeypatch, tmp_path):
    import fcntl
    cli, archive, calls, _sender = _health_followup_case(monkeypatch, tmp_path)
    with (archive / ".health-followup.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(["daily", "health-followup", "--date", "2026-10-02"]) == 0
    assert calls == []
    assert not (archive / ".health-card-sent").exists()
