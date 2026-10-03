from datetime import date, datetime, timedelta, time
import html
import json
import re
from types import SimpleNamespace

import httpx
from PIL import Image

from chat_daily_tg.config import EnergyUsage, HealthBriefing
from chat_daily_tg.energy_usage import BEIJING, DailyEnergy
from chat_daily_tg.health_briefing import ActivityDay, HealthExportReader, HealthReport, SleepEpisode
from chat_daily_tg.morning_card import build_panel, morning_caption, render_morning_card

ENTITY = EnergyUsage().entity_id
DAY = date(2026, 10, 2)
TZ = HealthExportReader("/tmp", "Asia/Shanghai").tz


def energy(values: dict[date, float]) -> dict[str, DailyEnergy]:
    return {
        day.isoformat(): DailyEnergy(
            day.isoformat(), kwh, ENTITY, datetime.combine(day, time(23, 59), BEIJING).timestamp(),
        )
        for day, kwh in values.items()
    }


def week_of_energy() -> dict[str, DailyEnergy]:
    kwh = [2.06, 1.93, 1.87, 1.33, 1.26, 1.04, 1.95, 2.31]
    return energy({DAY - timedelta(days=7 - i): v for i, v in enumerate(kwh)})


def health(synced: bool = True) -> HealthReport:
    episode = SleepEpisode(
        datetime(2026, 10, 3, 0, 5, tzinfo=TZ), datetime(2026, 10, 3, 7, 45, tzinfo=TZ),
        7.41, 0.26, 4.03, 0.88, 2.5,
    ) if synced else None
    return HealthReport(
        report_day=DAY, briefing_day=DAY + timedelta(days=1),
        activity=ActivityDay(None, None, None, None, None, 57, 47.5),
        sleep=episode, sleep_label="昨夜睡眠" if synced else "", wake_sleep=episode,
        workouts=(), medians=dict.fromkeys(["active", "exercise", "stand", "steps", "distance", "rhr", "hrv", "sleep", "wake"]),
        baseline_samples={}, baseline_days=28, min_baseline_samples=7,
    )


def visible(caption: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", caption))


def test_caption_summarises_energy_against_prior_week_and_sleep():
    panel = build_panel(DAY, health(), week_of_energy())
    assert panel.energy_baseline is not None and round(panel.energy_baseline, 2) == 1.63
    caption = morning_caption(panel)
    assert "<b>晨报 · 10月3日 周六</b>" in caption
    assert "用电 2.31 kWh，比前 7 日均值高 0.68；" in caption
    assert "实睡 7小时25分，起床 07:45。" in caption
    assert "<blockquote expandable>" in caption and "静息心率 57 bpm" in caption
    assert len(visible(caption)) <= 1024


def test_pending_sleep_and_missing_energy_are_explicit():
    records = week_of_energy()
    del records[DAY.isoformat()]
    panel = build_panel(DAY, health(synced=False), records)
    caption = morning_caption(panel)
    assert "用电数据暂缺" in caption
    assert "昨夜睡眠尚未同步，同步后更新本条。" in caption
    assert "实睡" not in caption


def test_short_history_skips_comparison():
    panel = build_panel(DAY, None, energy({DAY - timedelta(days=1): 1.0, DAY: 2.0}))
    assert panel.energy_baseline is None
    caption = morning_caption(panel)
    assert "用电 2.00 kWh；" in caption and "均值" not in caption
    assert "睡眠" not in caption


def test_caption_drops_detail_lines_to_fit_telegram_limit():
    panel = build_panel(DAY, health(), week_of_energy())
    long = panel.__class__(**{**panel.__dict__, "energy_periods": ["x" * 400] * 4})
    assert len(visible(morning_caption(long))) <= 1024


def test_render_produces_light_portrait_png(tmp_path):
    for synced in (True, False):
        path = render_morning_card(build_panel(DAY, health(synced), week_of_energy()),
                                   tmp_path / f"card-{synced}.png")
        with Image.open(path) as image:
            assert image.size == (1080, 1350)
            assert sum(image.getpixel((5, 5))) > 700  # light background
    assert render_morning_card(build_panel(DAY, None, None), tmp_path / "empty.png")


def test_send_morning_card_records_message_and_health_delivery(tmp_path):
    from chat_daily_tg import application

    sent = []
    tg = SimpleNamespace(send_photo=lambda path, **kw: sent.append((path, kw)) or 321)
    health_marker = tmp_path / ".health-card-sent"
    assert application._send_morning_card(
        tg, tmp_path, DAY, health(), week_of_energy(), health_marker,
    )
    state = json.loads((tmp_path / application.MORNING_CARD_MARKER).read_text())
    assert state["message_id"] == 321 and state["sleep"] is True
    assert health_marker.exists()
    assert sent[0][1]["parse_mode"] == "HTML"


def test_send_morning_card_failure_keeps_text_path(tmp_path):
    from chat_daily_tg import application

    def fail(*_a, **_kw):
        raise OSError("offline")

    tg = SimpleNamespace(send_photo=fail)
    assert not application._send_morning_card(
        tg, tmp_path, DAY, health(synced=False), week_of_energy(), tmp_path / ".h",
    )
    assert not (tmp_path / application.MORNING_CARD_MARKER).exists()


def _followup(monkeypatch, tmp_path, sender_cls, morning_state):
    from chat_daily_tg import cli
    import chat_daily_tg.health_briefing as hb
    import chat_daily_tg.tg_sender as ts

    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / ".run-complete").touch()
    (archive / ".morning-card-sent").write_text(json.dumps(morning_state))
    ledger = tmp_path / "daily.json"
    cfg = SimpleNamespace(
        health_briefing=HealthBriefing(enabled=True),
        energy_usage=EnergyUsage(enabled=True, ledger_path=ledger),
        schedule=SimpleNamespace(timezone="Asia/Shanghai"),
        telegram=SimpleNamespace(bot_token_env="T_TOKEN", chat_id_env="T_CHAT"),
        retry=SimpleNamespace(max_attempts=1, backoff_seconds=[]),
    )
    monkeypatch.setattr(cli.runtime, "prepare_process", lambda: None)
    monkeypatch.setattr(cli, "load_env_file", lambda _p: None)
    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "archive_dir_for", lambda _d: archive)
    monkeypatch.setattr(cli.runtime, "resolve_tg_target", lambda *a: ("-100", 7))
    monkeypatch.setenv("T_TOKEN", "unit-test-token")
    monkeypatch.setenv("T_CHAT", "unit-test-chat")
    monkeypatch.setattr(hb, "build_health_report", lambda *a: health())
    monkeypatch.setattr(ts, "TelegramSender", sender_cls)
    return cli, archive


class _Sender:
    calls: list = []

    def __init__(self, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def edit_photo(self, message_id, path, **kw):
        self.calls.append(("edit", message_id, kw["caption"]))

    def send_photo(self, path, **kw):
        self.calls.append(("photo", kw["caption"]))
        return 555

    def send_rich_message(self, **kw):
        raise AssertionError("morning card day must not send a rich health message")


def test_followup_edits_morning_card_in_place(monkeypatch, tmp_path):
    _Sender.calls = []
    cli, archive = _followup(monkeypatch, tmp_path, _Sender, {"message_id": 321, "sleep": False})
    args = ["daily", "health-followup", "--date", "2026-10-02"]
    assert cli.main(args) == 0
    assert [c[:2] for c in _Sender.calls] == [("edit", 321)]
    assert "起床 07:45" in _Sender.calls[0][2]
    assert (archive / ".health-card-sent").exists()
    assert json.loads((archive / ".morning-card-sent").read_text())["sleep"] is True
    assert cli.main(args) == 0 and len(_Sender.calls) == 1


def test_followup_sends_new_card_when_edit_rejected(monkeypatch, tmp_path):
    class Rejecting(_Sender):
        def edit_photo(self, *a, **kw):
            request = httpx.Request("POST", "https://api.telegram.org")
            raise httpx.HTTPStatusError(
                "bad", request=request, response=httpx.Response(400, request=request),
            )

    Rejecting.calls = []
    cli, archive = _followup(monkeypatch, tmp_path, Rejecting, {"message_id": 321})
    assert cli.main(["daily", "health-followup", "--date", "2026-10-02"]) == 0
    assert [c[0] for c in Rejecting.calls] == ["photo"]
    assert json.loads((archive / ".morning-card-sent").read_text())["message_id"] == 555
