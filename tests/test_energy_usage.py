from datetime import date, datetime, time, timedelta
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chat_daily_tg.config import EnergyUsage
from chat_daily_tg.energy_usage import (
    BEIJING,
    UNAVAILABLE,
    DailyEnergy,
    EnergyLedger,
    EnergyReading,
    SQLiteEnergySource,
    SSHEnergySource,
    build_energy_briefing,
    daily_closings,
    format_energy_briefing,
)

ENTITY = EnergyUsage().entity_id


def reading(day, clock, state):
    timestamp = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=BEIJING).timestamp()
    return EnergyReading(state, timestamp)


def record(day, value):
    timestamp = datetime.combine(day, time(23, 59), BEIJING).timestamp()
    return DailyEnergy(day.isoformat(), value, ENTITY, timestamp)


def period(start, end, value):
    rows = {}
    while start <= end:
        rows[start.isoformat()] = record(start, value)
        start += timedelta(days=1)
    return rows


def recorder(tmp_path, readings):
    path = tmp_path / "recorder.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
        db.execute(
            "CREATE TABLE states (state_id INTEGER PRIMARY KEY, metadata_id INTEGER, "
            "state TEXT, last_updated_ts REAL)"
        )
        db.execute("INSERT INTO states_meta VALUES (1, ?)", (ENTITY,))
        db.executemany(
            "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (1, ?, ?)",
            [(row.state, row.timestamp) for row in readings],
        )
    return path


def test_close_extends_to_next_day_and_stops_at_reset():
    rows = [
        reading("2026-10-02", "23:58", "2.29"),
        reading("2026-10-03", "00:02", "2.31"),
        reading("2026-10-03", "00:05", "0"),
        reading("2026-10-03", "00:08", "0.02"),
        reading("2026-10-03", "00:11", "0.03"),
    ]
    closing = daily_closings(rows, date(2026, 10, 2), date(2026, 10, 2), ENTITY)
    assert len(closing) == 1
    assert closing[0].kwh == 2.31
    assert closing[0].last_updated == rows[1].timestamp


def test_positive_post_reset_value_cannot_replace_close_when_zero_row_missing():
    rows = [
        reading("2026-10-02", "23:59", "2.31"),
        reading("2026-10-03", "00:05", "0.01"),
        reading("2026-10-03", "00:09", "0.02"),
    ]
    assert daily_closings(rows, date(2026, 10, 2), date(2026, 10, 2), ENTITY)[0].kwh == 2.31


def test_zero_usage_is_valid_but_next_day_reset_alone_is_not_a_closing():
    target = date(2026, 10, 2)
    zero = reading("2026-10-02", "23:59", "0")
    reset = reading("2026-10-03", "00:05", "0")
    assert daily_closings([zero, reset], target, target, ENTITY)[0].kwh == 0
    assert daily_closings([reset], target, target, ENTITY) == []


def test_closing_skips_invalid_states_and_obeys_beijing_window():
    target = date(2026, 10, 2)
    rows = [
        reading("2026-10-01", "23:59", "8"),
        reading("2026-10-02", "23:58", "2.31"),
        *[reading("2026-10-02", "23:59", value) for value in (
            "unknown", "unavailable", "-1", "NaN", "inf",
        )],
        reading("2026-10-03", "00:10", "2.32"),
        reading("2026-10-03", "00:10:01", "9"),
    ]
    assert daily_closings(list(reversed(rows)), target, target, ENTITY)[0].kwh == 2.32


def test_sqlite_source_is_readonly_and_missing_entity_returns_no_data(tmp_path):
    path = recorder(tmp_path, [
        reading("2026-10-02", "23:59", "2.31"),
        reading("2026-10-03", "00:05", "0"),
    ])
    before = path.read_bytes()
    source = SQLiteEnergySource(path.as_uri() + "?mode=ro")
    start = datetime(2026, 10, 2, tzinfo=BEIJING)
    end = datetime(2026, 10, 3, 0, 10, tzinfo=BEIJING)
    assert [row.state for row in source.read(ENTITY, start, end)] == ["2.31", "0"]
    assert source.read("sensor.missing", start, end) == []
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*-journal"))
    with pytest.raises(ValueError, match="mode=ro"):
        SQLiteEnergySource(path.as_uri() + "?mode=rw")


def test_missing_sqlite_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    source = SQLiteEnergySource(path.as_uri() + "?mode=ro")
    with pytest.raises(sqlite3.OperationalError):
        source.read(ENTITY, datetime(2026, 10, 2, tzinfo=BEIJING), datetime(2026, 10, 3, tzinfo=BEIJING))
    assert not path.exists()


def test_ssh_reader_script_executes_against_offline_sqlite_without_ssh(tmp_path, monkeypatch):
    path = recorder(tmp_path, [reading("2026-10-02", "23:59", "2.31")])
    cfg = EnergyUsage(enabled=True, recorder_uri=path.as_uri() + "?mode=ro", timeout_seconds=9)
    actual_run = subprocess.run
    calls = []

    def offline_run(command, **kwargs):
        import shlex

        calls.append((command, kwargs))
        remote = shlex.split(command[-1])
        return actual_run([sys.executable, *remote[1:]], **kwargs)

    monkeypatch.setattr("chat_daily_tg.energy_usage.subprocess.run", offline_run)
    rows = SSHEnergySource(cfg).read(
        ENTITY, datetime(2026, 10, 2, tzinfo=BEIJING),
        datetime(2026, 10, 3, 0, 10, tzinfo=BEIJING),
    )
    assert rows[0].state == "2.31"
    assert calls[0][0][:3] == ["ssh", "-o", "BatchMode=yes"]
    assert calls[0][0][-2] == "r4s"
    assert calls[0][1]["timeout"] == 9
    assert "PRAGMA query_only=ON" in calls[0][1]["input"]
    assert "mode=ro" in calls[0][0][-1]


def test_ledger_atomic_replace_idempotence_and_missing_days(tmp_path, monkeypatch):
    import chat_daily_tg.energy_usage as energy

    path = tmp_path / "energy" / "daily.json"
    ledger = EnergyLedger(path)
    actual_replace = energy.os.replace
    replaces = []

    def checked_replace(source, destination):
        payload = json.loads(source.read_text())
        assert payload["days"]["2026-10-02"]["kwh"] == 2.31
        assert destination == path
        assert source.parent == path.parent
        replaces.append((source, destination))
        actual_replace(source, destination)

    monkeypatch.setattr(energy.os, "replace", checked_replace)
    row = record(date(2026, 10, 2), 2.31)
    assert ledger.update([row]) == {"2026-10-02": row}
    before = path.stat().st_mtime_ns
    ledger.update([row])
    assert path.stat().st_mtime_ns == before
    assert len(replaces) == 1
    assert set(ledger.read()) == {"2026-10-02"}
    assert not list(path.parent.glob("*.tmp"))


def test_failed_atomic_replace_keeps_existing_ledger(tmp_path, monkeypatch):
    ledger = EnergyLedger(tmp_path / "daily.json")
    original = record(date(2026, 10, 1), 1.0)
    ledger.update([original])
    before = ledger.path.read_bytes()
    monkeypatch.setattr(
        "chat_daily_tg.energy_usage.os.replace", Mock(side_effect=OSError("write failed")),
    )
    with pytest.raises(OSError, match="write failed"):
        ledger.update([record(date(2026, 10, 2), 2.31)])
    assert ledger.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_ledger_retains_newer_closings_after_recorder_history_expires(tmp_path):
    ledger = EnergyLedger(tmp_path / "daily.json")
    original = record(date(2026, 10, 2), 2.31)
    ledger.update([original])
    earlier = DailyEnergy(original.date, 2.0, ENTITY, original.last_updated - 60)
    assert ledger.update([earlier])[original.date] == original
    assert ledger.update([])[original.date] == original


def test_daily_mean_needs_three_days_and_reports_absolute_change():
    target = date(2026, 10, 2)
    rows = period(date(2026, 9, 29), target, 1.0)
    rows[target.isoformat()] = record(target, 2.5)
    text = format_energy_briefing(target, rows)
    assert "2026-10-02" in text and "2.50 kWh" in text
    assert "前 7 日均值 1.00 kWh（3/7 天）" in text
    assert "较均值 +1.50 kWh" in text
    del rows["2026-09-29"]
    assert "均值" not in format_energy_briefing(target, rows)


def test_monday_reports_previous_complete_week_and_comparison():
    rows = period(date(2026, 9, 28), date(2026, 10, 4), 1.0)
    rows.update(period(date(2026, 10, 5), date(2026, 10, 11), 2.0))
    rows["2026-10-11"] = record(date(2026, 10, 11), 3.0)
    text = format_energy_briefing(date(2026, 10, 11), rows)
    assert "上周（2026-10-05–2026-10-11，完整周）" in text
    assert "总量 15.00 kWh，日均 2.14 kWh" in text
    assert "最高 2026-10-11 3.00 kWh" in text
    assert "最低 2026-10-05 2.00 kWh" in text
    assert "较前周 +8.00 kWh" in text
    assert "上周" not in format_energy_briefing(date(2026, 10, 10), rows)


def test_missing_week_day_is_partial_and_suppresses_comparison():
    rows = period(date(2026, 9, 28), date(2026, 10, 11), 1.0)
    del rows["2026-10-06"]
    text = format_energy_briefing(date(2026, 10, 11), rows)
    assert "数据 6/7 天" in text
    assert "完整周" not in text
    assert "较前周" not in text
    assert "有效日均 1.00 kWh" in text


def test_month_start_reports_previous_month_daily_details_and_comparison():
    rows = period(date(2026, 9, 1), date(2026, 9, 30), 2.0)
    rows.update(period(date(2026, 10, 1), date(2026, 10, 31), 1.0))
    text = format_energy_briefing(date(2026, 10, 31), rows)
    assert "上月（2026-10-01–2026-10-31，完整月）" in text
    assert "总量 31.00 kWh，日均 1.00 kWh" in text
    assert "较前月 -29.00 kWh" in text
    assert "上月每日明细" in text
    assert text.count("  - 2026-10-") == 31
    assert "上月" not in format_energy_briefing(date(2026, 10, 30), rows)


def test_partial_month_shows_missing_dates_without_extrapolation():
    rows = {"2026-10-31": record(date(2026, 10, 31), 2.31)}
    text = format_energy_briefing(date(2026, 10, 31), rows)
    assert "数据 1/31 天" in text
    assert "总量 2.31 kWh" in text
    assert "2026-10-01：暂缺" in text
    assert "完整月" not in text
    assert "较前月" not in text


def test_leap_month_and_year_boundary_use_calendar_days():
    leap = period(date(2028, 2, 1), date(2028, 2, 29), 1)
    assert "总量 29.00 kWh" in format_energy_briefing(date(2028, 2, 29), leap)
    december = period(date(2026, 12, 1), date(2026, 12, 31), 1)
    assert "上月（2026-12-01–2026-12-31" in format_energy_briefing(date(2026, 12, 31), december)


def test_build_backfills_history_and_repeat_run_preserves_ledger(tmp_path):
    path = recorder(tmp_path, [
        reading("2026-09-30", "23:59", "1.04"),
        reading("2026-10-01", "00:05", "0"),
        reading("2026-10-01", "23:59", "1.95"),
        reading("2026-10-02", "00:05", "0"),
        reading("2026-10-02", "23:59", "2.31"),
        reading("2026-10-03", "00:05", "0"),
    ])
    source = SQLiteEnergySource(path.as_uri() + "?mode=ro")
    ledger = EnergyLedger(tmp_path / "daily.json")
    cfg = EnergyUsage(enabled=True)
    text = build_energy_briefing(date(2026, 10, 2), cfg, source=source, ledger=ledger)
    # Only two days precede 10-02, below the three-day baseline minimum.
    assert "2.31 kWh" in text and "均值" not in text
    assert set(ledger.read()) == {"2026-09-30", "2026-10-01", "2026-10-02"}
    before = ledger.path.read_bytes()
    assert build_energy_briefing(date(2026, 10, 2), cfg, source=source, ledger=ledger) == text
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("error", [None, OSError("offline"), subprocess.TimeoutExpired("ssh", 1)])
def test_source_missing_or_unreachable_does_not_block_briefing(tmp_path, error):
    source = Mock()
    source.read.return_value = []
    source.read.side_effect = error
    ledger = EnergyLedger(tmp_path / "daily.json")
    text = build_energy_briefing(
        date(2026, 10, 2), EnergyUsage(enabled=True), source=source, ledger=ledger,
    )
    assert "用电数据暂缺" in text
    assert not ledger.path.exists()


def test_ledger_write_failure_still_displays_measured_usage(tmp_path, monkeypatch):
    source = Mock()
    source.read.return_value = [reading("2026-10-02", "23:59", "2.31")]
    ledger = EnergyLedger(tmp_path / "daily.json")
    monkeypatch.setattr(ledger, "update", Mock(side_effect=OSError("disk full")))
    text = build_energy_briefing(
        date(2026, 10, 2), EnergyUsage(enabled=True), source=source, ledger=ledger,
    )
    assert "昨日用电（2026-10-02）：2.31 kWh" in text


def test_corrupt_ledger_is_preserved_and_report_degrades(tmp_path):
    path = tmp_path / "daily.json"
    path.write_text("broken", encoding="utf-8")
    source = Mock()
    source.read.return_value = [reading("2026-10-02", "23:59", "2.31")]
    assert build_energy_briefing(
        date(2026, 10, 2), EnergyUsage(enabled=True), source=source, ledger=EnergyLedger(path),
    ) == UNAVAILABLE
    assert path.read_text() == "broken"


def test_disabled_energy_does_not_query_or_write(tmp_path):
    source, ledger = Mock(), EnergyLedger(tmp_path / "daily.json")
    assert build_energy_briefing(date(2026, 10, 2), EnergyUsage(), source=source, ledger=ledger) == ""
    source.read.assert_not_called()
    assert not ledger.path.exists()


@pytest.mark.parametrize("health_enabled", [True, False])
@pytest.mark.parametrize("energy_error", [None, OSError("offline")])
def test_daily_orchestration_archives_energy_after_health_and_survives_failure(
    tmp_path, monkeypatch, health_enabled, energy_error,
):
    from chat_daily_tg import application, paths
    from chat_daily_tg.config import Config
    from chat_daily_tg.summarizer import SummaryOutput

    cfg = Config(
        groups=["fixture"],
        llm={"endpoint": "http://fixture", "model": "fixture", "api_key_env": "FIXTURE_KEY"},
        telegram={"bot_token_env": "FIXTURE_TG", "chat_id_env": "FIXTURE_CHAT"},
        health_briefing={"enabled": health_enabled},
        energy_usage={"enabled": True},
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(application, "DATA_DIR", tmp_path)
    monkeypatch.setattr(application, "load_config", lambda *_: cfg)
    monkeypatch.setattr(application, "prepare_archive_day", lambda *_: archive)
    monkeypatch.setattr(application, "cleanup_old_media", lambda *_: (0, 0))
    monkeypatch.setattr(application, "_export_wechat_lane", lambda *_a, **_kw: ([("fixture", "text")], []))
    monkeypatch.setattr(application, "_llm_from_block", lambda *_: Mock(model="fixture"))
    monkeypatch.setattr(application, "_persist_opportunities", Mock())
    monkeypatch.setattr(application, "notify_failure", Mock())
    monkeypatch.setattr(application, "TelegramSender", Mock(side_effect=AssertionError("no Telegram")))
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "fixture.db")
    for name in (
        "active_permanent_summary", "active_hot_leads_summary", "active_repeat_topics_summary",
    ):
        monkeypatch.setattr(f"chat_daily_tg.context_builder.{name}", lambda *_a, **_kw: "")
    monkeypatch.setattr("chat_daily_tg.permanent_md.regenerate_permanent_md", Mock())
    monkeypatch.setattr("chat_daily_tg.hot_leads.regenerate_latest", Mock())
    monkeypatch.setattr("chat_daily_tg.dedup_journal.today_counts", lambda: {})
    health = "### 个人晨报\n- 活动：fixture"
    health_report = SimpleNamespace(last_night_sleep=object(), data_gaps=(), sleep_label="昨夜睡眠")
    monkeypatch.setattr("chat_daily_tg.health_briefing.build_health_report", lambda *_: health_report)
    monkeypatch.setattr("chat_daily_tg.health_briefing.format_health_briefing", lambda *_: health)
    monkeypatch.setattr("chat_daily_tg.health_briefing.write_health_gap_record", Mock())
    monkeypatch.setattr("chat_daily_tg.health_card.render_health_card", lambda *_: None)
    monkeypatch.setattr("chat_daily_tg.health_rich.build_health_rich_markdown", lambda *_a, **_kw: health)
    summary = "### 今日总览\n" + "正常日报正文。" * 30
    monkeypatch.setattr(
        application, "_run_summary_with_fallback",
        lambda *_a, **_kw: SummaryOutput(summary, "details", {}),
    )
    energy = "### ⚡ 小米插座用电\n- 昨日用电（2026-10-02）：2.31 kWh"
    builder = Mock(return_value={"2026-10-02": record(date(2026, 10, 2), 2.31)},
                   side_effect=energy_error)
    monkeypatch.setattr("chat_daily_tg.energy_usage.refresh_energy_records", builder)

    assert application._run("2026-10-02", no_push=True) == 0
    expected = UNAVAILABLE if energy_error else energy
    text = (archive / "concise.md").read_text()
    assert (archive / "energy-briefing.md").read_text() == expected
    assert expected in text and summary in text
    if health_enabled:
        assert text.startswith(health + "\n\n" + expected)
    else:
        assert text.startswith(expected)
    builder.assert_called_once_with(date(2026, 10, 2), cfg.energy_usage)
    application.TelegramSender.assert_not_called()
    assert not (archive / application.COMPLETE_MARKER).exists()


def test_pending_sleep_does_not_mark_health_delivered(tmp_path, monkeypatch):
    from chat_daily_tg import application, paths
    from chat_daily_tg.config import Config
    from chat_daily_tg.summarizer import SummaryOutput

    cfg = Config(
        groups=["fixture"],
        llm={"endpoint": "http://fixture", "model": "fixture", "api_key_env": "FIXTURE_KEY"},
        telegram={"bot_token_env": "FIXTURE_TG", "chat_id_env": "FIXTURE_CHAT"},
        health_briefing={"enabled": True},
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    report = SimpleNamespace(last_night_sleep=None, data_gaps=(), sleep_label="待同步")
    monkeypatch.setattr(application, "DATA_DIR", tmp_path)
    monkeypatch.setattr(application, "load_config", lambda *_: cfg)
    monkeypatch.setattr(application, "prepare_archive_day", lambda *_: archive)
    monkeypatch.setattr(application, "cleanup_old_media", lambda *_: (0, 0))
    monkeypatch.setattr(application, "_export_wechat_lane", lambda *_a, **_kw: ([("fixture", "text")], []))
    monkeypatch.setattr(application, "_llm_from_block", lambda *_: Mock(model="fixture"))
    monkeypatch.setattr(application, "_persist_opportunities", Mock())
    monkeypatch.setattr(application, "notify_failure", Mock())
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "fixture.db")
    for name in ("active_permanent_summary", "active_hot_leads_summary", "active_repeat_topics_summary"):
        monkeypatch.setattr(f"chat_daily_tg.context_builder.{name}", lambda *_a, **_kw: "")
    monkeypatch.setattr("chat_daily_tg.permanent_md.regenerate_permanent_md", Mock())
    monkeypatch.setattr("chat_daily_tg.hot_leads.regenerate_latest", Mock())
    monkeypatch.setattr("chat_daily_tg.dedup_journal.today_counts", lambda: {})
    monkeypatch.setattr("chat_daily_tg.health_briefing.build_health_report", lambda *_: report)
    monkeypatch.setattr("chat_daily_tg.health_briefing.format_health_briefing", lambda *_: "健康待补")
    monkeypatch.setattr("chat_daily_tg.health_briefing.write_health_gap_record", Mock())
    monkeypatch.setattr("chat_daily_tg.health_card.render_health_card", lambda *_: archive / "health-card.png")
    monkeypatch.setattr("chat_daily_tg.health_rich.build_health_rich_markdown", lambda *_a, **_kw: "健康待补")
    monkeypatch.setattr(
        application, "_run_summary_with_fallback",
        lambda *_a, **_kw: SummaryOutput("### 今日总览\n" + "正常日报正文。" * 30, "details", {}),
    )
    sender = Mock()
    sender.send_rich_message.return_value = {"ok": True}
    monkeypatch.setattr(application, "TelegramSender", Mock(return_value=sender))
    monkeypatch.setattr(application, "resolve_tg_target", lambda *_: ("1", None))
    monkeypatch.setenv("FIXTURE_TG", "token")
    monkeypatch.setenv("FIXTURE_CHAT", "1")

    assert application._run("2026-10-02") == 0
    assert not (archive / ".health-card-sent").exists()
