from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import re
from types import SimpleNamespace
import pytest

from pytest_httpx import HTTPXMock

from chat_daily_tg.growth_store import (
    LOCAL_TZ,
    GrowthSegment,
    ensure_rubric,
    insert_segments,
    log_ab,
    mark_sent,
    segment_id,
)
from chat_daily_tg.growth_weekly import (
    build_weekly_report,
    consume_inbox,
    merge_rubric,
    poll_dm_feedback,
)

from chat_daily_tg.growth_weekly import _feedback_relay_config_path as _REAL_RELAY_CONFIG_PATH

DM_CHAT_ID = "999888777"
GETUPDATES_RE = re.compile(r"https://api\.telegram\.org/bot-TOKEN-/getUpdates.*")




@pytest.fixture(autouse=True)
def _isolated_feedback_relay_config(tmp_path, monkeypatch):
    from chat_daily_tg import growth_weekly
    monkeypatch.setattr(growth_weekly, "_feedback_relay_config_path",
                        lambda: tmp_path / "missing-relay.json")


class FakeLLM:
    """Mirrors LLMClient.chat(prompt, system=None) -> (text, usage)."""

    def __init__(self, response: str = ""):
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        self.calls.append((prompt, system))
        return self.response, {}


def _update(update_id: int, chat_id: int, text: str, chat_type: str = "private", date: int = 1751990000):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id - 500,
            "date": date,
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


# --------------------------------------------------------------- poll_dm_feedback

def test_poll_two_batches_then_empty_and_second_poll_resumes(tmp_path: Path, httpx_mock: HTTPXMock):
    offset_path = tmp_path / "offset.txt"
    inbox_path = tmp_path / "inbox.jsonl"

    batch1 = {"ok": True, "result": [
        _update(501, int(DM_CHAT_ID), "反馈1"),
        _update(502, 111222333, "群里消息", chat_type="supergroup"),  # not the DM chat
    ]}
    batch2 = {"ok": True, "result": [_update(503, int(DM_CHAT_ID), "反馈2")]}
    empty = {"ok": True, "result": []}

    httpx_mock.add_response(url=GETUPDATES_RE, json=batch1)
    httpx_mock.add_response(url=GETUPDATES_RE, json=batch2)
    httpx_mock.add_response(url=GETUPDATES_RE, json=empty)

    count = poll_dm_feedback("-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path)

    assert count == 2
    assert offset_path.read_text(encoding="utf-8").strip() == "503"
    lines = inbox_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [p["text"] for p in parsed] == ["反馈1", "反馈2"]
    assert [p["update_id"] for p in parsed] == [501, 503]

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 3
    assert reqs[0].url.params["offset"] == "1"          # no prior offset -> saved(0)+1
    assert reqs[0].url.params["timeout"] == "0"
    assert reqs[0].url.params["allowed_updates"] == '["message"]'
    assert reqs[1].url.params["offset"] == "503"        # after batch1 high-water 502 -> +1
    assert reqs[2].url.params["offset"] == "504"        # after batch2 high-water 503 -> +1

    # A second, independent poll call must resume from the persisted offset.
    httpx_mock.add_response(url=GETUPDATES_RE, json=empty)
    count2 = poll_dm_feedback("-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path)
    assert count2 == 0
    reqs_all = httpx_mock.get_requests()
    assert reqs_all[-1].url.params["offset"] == "504"


def test_poll_409_returns_zero_without_raising_and_offset_untouched(tmp_path: Path, httpx_mock: HTTPXMock):
    offset_path = tmp_path / "offset.txt"
    inbox_path = tmp_path / "inbox.jsonl"
    httpx_mock.add_response(
        url=GETUPDATES_RE, status_code=409,
        json={"ok": False, "error_code": 409, "description": "Conflict"})

    count = poll_dm_feedback("-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path)

    assert count == 0
    assert not offset_path.exists()
    assert not inbox_path.exists()


# ------------------------------------------------------------------- consume_inbox

def test_consume_inbox_dedupes_sorts_and_renames(tmp_path: Path):
    inbox = tmp_path / "feedback-inbox.jsonl"
    inbox.write_text(
        json.dumps({"update_id": 502, "date": 1751990010, "text": "反馈2"}) + "\n"
        + json.dumps({"update_id": 501, "date": 1751990000, "text": "反馈1"}) + "\n"
        + json.dumps({"update_id": 501, "date": 1751990000, "text": "反馈1"}) + "\n",  # duplicate
        encoding="utf-8")

    entries = consume_inbox(inbox)

    assert [e["update_id"] for e in entries] == [501, 502]
    assert [e["text"] for e in entries] == ["反馈1", "反馈2"]
    assert not inbox.exists()
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    assert (tmp_path / f"feedback-processed-{today}.jsonl").exists()


def test_consume_inbox_missing_file_returns_empty(tmp_path: Path):
    assert consume_inbox(tmp_path / "nope.jsonl") == []


def test_consume_inbox_empty_file_returns_empty_without_rename(tmp_path: Path):
    inbox = tmp_path / "feedback-inbox.jsonl"
    inbox.write_text("", encoding="utf-8")
    assert consume_inbox(inbox) == []
    assert inbox.exists()  # nothing to process -> left alone


# --------------------------------------------------------------------- merge_rubric

def test_merge_rubric_enforces_header_when_llm_omits_it(tmp_path: Path):
    rubric_path = tmp_path / "rubric.md"
    history_dir = tmp_path / "history"
    old_text, old_version = ensure_rubric(rubric_path)  # creates default v1
    assert old_version == "v1"

    # no header line, but long enough to clear merge_rubric's >=40-char sanity floor
    llm = FakeLLM(response="- 反馈已经吸收：以后要求更短更硬\n- 保持金句必须逐字原话\n- 语气克制，不用感叹号堆情绪\n")
    new_text, new_version, changed = merge_rubric(
        llm, rubric_path, history_dir, ["太啰嗦了，以后短一点"])

    assert changed is True
    assert new_version == "v2"
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    assert new_text.splitlines()[0] == f"# 成长卡片评审偏好 v2（{today}）"
    assert "以后要求更短更硬" in new_text
    assert rubric_path.read_text(encoding="utf-8") == new_text + "\n"
    assert len(llm.calls) == 1
    prompt, system = llm.calls[0]
    assert "太啰嗦了，以后短一点" in prompt
    assert old_text in prompt

    hist_files = list(history_dir.glob("rubric-v1-*.md"))
    assert len(hist_files) == 1
    assert hist_files[0].read_text(encoding="utf-8") == old_text


def test_merge_rubric_empty_feedback_skips_llm_call(tmp_path: Path):
    rubric_path = tmp_path / "rubric.md"
    history_dir = tmp_path / "history"
    text, version = ensure_rubric(rubric_path)
    llm = FakeLLM(response="should never be used")

    new_text, new_version, changed = merge_rubric(llm, rubric_path, history_dir, [])

    assert (new_text, new_version, changed) == (text, version, False)
    assert llm.calls == []
    assert not history_dir.exists()


def test_merge_rubric_whitespace_llm_output_keeps_current(tmp_path: Path):
    rubric_path = tmp_path / "rubric.md"
    history_dir = tmp_path / "history"
    text, version = ensure_rubric(rubric_path)
    llm = FakeLLM(response="   \n\n   ")

    new_text, new_version, changed = merge_rubric(llm, rubric_path, history_dir, ["随便的反馈"])

    assert new_text == text
    assert new_version == version
    assert changed is False
    assert len(llm.calls) == 1
    assert rubric_path.read_text(encoding="utf-8") == text  # untouched
    assert not history_dir.exists()  # nothing was overwritten, no backup taken


# ---------------------------------------------------------------- build_weekly_report

def _seg(date: str, start: int, end: int, **kw) -> GrowthSegment:
    return GrowthSegment(
        id=segment_id(date, start), chat_id=1162433032, chat_name="电丸朱氏会社",
        date=date, start_msg_id=start, end_msg_id=end,
        start_hm="22:22", end_hm="23:07", msg_count=end - start + 1,
        theme=kw.pop("theme", "回本心态与价值创造"),
        points=kw.pop("points", ["价值是创造出来的，不是节省出来的"]),
        quotes=[{"msg_id": start + 5, "sender": "A K", "text": "价值是创造出来的 不是节省出来的"}],
        participants="A K", score=kw.pop("score", 8.0), status=kw.pop("status", "pending"), **kw)


def test_build_weekly_report_with_seeded_data(tmp_path: Path):
    db = tmp_path / "t.db"
    seg = _seg("2026-07-08", 100, 200)
    insert_segments(db, [seg])
    mark_sent(db, seg.id, style="A", sent_at=datetime.now(LOCAL_TZ).isoformat(timespec="seconds"))
    log_ab(db, seg.id, "v1", "A", 8.5, 6.0, "更精炼、更贴近处境",
           "<b>卡A标题</b>\n要点1", "<b>卡B标题</b>\n要点1（改写版）")

    llm = FakeLLM(response="本周主题集中在个人成长叙事，重复度较低，风格保持一致，未见明显漂移。")
    html = build_weekly_report(db, 1162433032, llm, "v2", True)

    assert "🌱 成长挖掘周报" in html
    assert "待发 0 · 已发 1 · 已拒 0" in html
    assert "A/B 胜出（7天）：A 1 : B 0" in html
    assert "A/B 胜出（累计）：A 1 : B 0" in html
    assert "本周主题集中在个人成长叙事" in html
    assert "&lt;b&gt;卡A标题&lt;/b&gt;" in html
    assert "&lt;b&gt;卡B标题&lt;/b&gt;" in html
    assert "<b>卡A标题</b>" not in html  # raw HTML from the card must not survive unescaped
    assert "更精炼、更贴近处境" in html
    assert "胜出：A（A 8.5 : B 6.0）" in html
    assert "评审版本：v2（本周已按你的反馈更新）" in html
    assert len(llm.calls) == 1


def test_build_weekly_report_empty_db_skips_llm_call(tmp_path: Path):
    db = tmp_path / "empty.db"
    llm = FakeLLM(response="should never be used")

    html = build_weekly_report(db, 1162433032, llm, "v1", False)

    assert "本周暂无已发送卡片" in html
    assert "待发 0 · 已发 0 · 已拒 0" in html
    assert "暂无 A/B 评审样例" in html
    assert "评审版本：v1" in html
    assert "本周已按你的反馈更新" not in html
    assert llm.calls == []




def _enable_relay(tmp_path, monkeypatch, *, owner=DM_CHAT_ID):
    from chat_daily_tg import growth_weekly
    config_path = tmp_path / "x-review-feedback-relay.json"
    config_path.write_text(json.dumps({
        "enabled": True,
        "host": "bwg",
        "project_dir": "/root/x_monitor",
        "state_path": "/root/x_monitor/state/x-review-feedback.sqlite3",
        "owner": owner,
    }), encoding="utf-8")
    monkeypatch.setattr(growth_weekly, "_feedback_relay_config_path", lambda: config_path)
    return config_path


def test_relay_success_appends_and_persists_update_offset(tmp_path, monkeypatch):
    from chat_daily_tg import growth_weekly
    _enable_relay(tmp_path, monkeypatch)
    offset_path = tmp_path / "offset.txt"
    offset_path.write_text("40", encoding="utf-8")
    inbox_path = tmp_path / "inbox.jsonl"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[:2] == ["ssh", "-o"]
        assert "relay" in argv[-1] and "--after 40" in argv[-1]
        assert "--owner 999888777" in argv[-1]
        return SimpleNamespace(stdout=json.dumps([
            {"id": 51, "update_id": 41, "date": 1751990000, "text": "通过 relay 的反馈"},
        ]))

    monkeypatch.setattr(growth_weekly.subprocess, "run", fake_run)
    assert growth_weekly.poll_dm_feedback(
        "-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path
    ) == 1
    assert offset_path.read_text(encoding="utf-8") == "41"
    assert [json.loads(line)["text"] for line in inbox_path.read_text().splitlines()] == ["通过 relay 的反馈"]
    assert len(calls) == 1


def test_relay_failure_preserves_offset_and_never_polls_getupdates(tmp_path, monkeypatch):
    from chat_daily_tg import growth_weekly
    _enable_relay(tmp_path, monkeypatch)
    offset_path = tmp_path / "offset.txt"
    offset_path.write_text("40", encoding="utf-8")
    inbox_path = tmp_path / "inbox.jsonl"

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("ssh unavailable")

    class NoDirectClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("relay failure must not steal getUpdates")

    monkeypatch.setattr(growth_weekly.subprocess, "run", fail_run)
    monkeypatch.setattr(growth_weekly.httpx, "Client", NoDirectClient)
    assert growth_weekly.poll_dm_feedback(
        "-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path
    ) == 0
    assert offset_path.read_text(encoding="utf-8") == "40"
    assert not inbox_path.exists()


def test_relay_owner_mismatch_is_rejected_without_ssh_or_cursor_change(tmp_path, monkeypatch):
    from chat_daily_tg import growth_weekly
    _enable_relay(tmp_path, monkeypatch, owner="123")
    offset_path = tmp_path / "offset.txt"
    offset_path.write_text("40", encoding="utf-8")
    inbox_path = tmp_path / "inbox.jsonl"
    monkeypatch.setattr(
        growth_weekly.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("owner mismatch must not invoke ssh"),
    )
    assert growth_weekly.poll_dm_feedback(
        "-TOKEN-", DM_CHAT_ID, offset_path=offset_path, inbox_path=inbox_path
    ) == 0
    assert offset_path.read_text(encoding="utf-8") == "40"
    assert not inbox_path.exists()


def test_relay_config_uses_home_state_path(monkeypatch, tmp_path):
    from chat_daily_tg import growth_weekly
    monkeypatch.setattr(growth_weekly.Path, "home", lambda: tmp_path)
    assert _REAL_RELAY_CONFIG_PATH() == (
        tmp_path / "chat-daily/state/x-review-feedback-relay.json")


def test_disabled_relay_keeps_direct_poll(tmp_path, monkeypatch, httpx_mock):
    path = _enable_relay(tmp_path, monkeypatch)
    path.write_text('{"enabled": false}', encoding="utf-8")
    httpx_mock.add_response(url=GETUPDATES_RE, json={"ok": True, "result": []})
    assert poll_dm_feedback("-TOKEN-", DM_CHAT_ID,
        offset_path=tmp_path / "offset", inbox_path=tmp_path / "inbox") == 0
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("case", ["bad-owner", "bad-host", "other-owner-row", "malformed-row", "malformed-config"])
def test_relay_rejects_unsafe_or_malformed_input_without_state_changes(tmp_path, monkeypatch, case):
    from chat_daily_tg import growth_weekly
    path = _enable_relay(tmp_path, monkeypatch)
    config = json.loads(path.read_text())
    rows = [{"id": 51, "update_id": 41, "date": 1751990000, "text": "feedback"}]
    dm = DM_CHAT_ID
    if case == "bad-owner":
        dm = "999888777; touch /tmp/unsafe"
    elif case == "bad-host":
        config["host"] = "-oProxyCommand=touch /tmp/unsafe"
    elif case == "other-owner-row":
        rows[0]["chat_id"] = "123"
    elif case == "malformed-row":
        rows.append({"update_id": 42})
    path.write_text("{" if case == "malformed-config" else json.dumps(config))
    calls = []
    monkeypatch.setattr(growth_weekly.subprocess, "run", lambda *a, **kw:
        (calls.append(a) or SimpleNamespace(stdout=json.dumps(rows))))
    offset = tmp_path / "offset"
    offset.write_text("40")
    inbox = tmp_path / "inbox"
    assert poll_dm_feedback("-TOKEN-", dm, offset_path=offset, inbox_path=inbox) == 0
    assert offset.read_text() == "40" and not inbox.exists()
    if case in {"bad-owner", "bad-host", "malformed-config"}:
        assert calls == []


def test_relay_quotes_remote_paths_and_ignores_replayed_rows(tmp_path, monkeypatch):
    import shlex
    from chat_daily_tg import growth_weekly
    path = _enable_relay(tmp_path, monkeypatch)
    config = json.loads(path.read_text())
    config["project_dir"] = "/root/x monitor; echo literal"
    config["state_path"] = "/root/data ' state.sqlite3"
    path.write_text(json.dumps(config))
    offset = tmp_path / "offset"
    offset.write_text("40")
    inbox = tmp_path / "inbox"

    def run(argv, **kwargs):
        command = shlex.split(argv[-1])
        assert command == ["cd", config["project_dir"], "&&", "python3", "x_review_bot.py",
            "--state", config["state_path"], "relay", "--after", "40", "--owner", DM_CHAT_ID]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
        return SimpleNamespace(stdout=json.dumps([
            {"id": 50, "update_id": 40, "date": 1751990000, "text": "old"},
            {"id": 52, "update_id": 42, "date": 1751990002, "text": "second"},
            {"id": 51, "update_id": 41, "date": 1751990001, "text": "first"},
            {"id": 51, "update_id": 41, "date": 1751990001, "text": "first"},
        ]))

    monkeypatch.setattr(growth_weekly.subprocess, "run", run)
    assert poll_dm_feedback("-TOKEN-", DM_CHAT_ID, offset_path=offset, inbox_path=inbox) == 2
    assert offset.read_text() == "42"
    assert [json.loads(line)["update_id"] for line in inbox.read_text().splitlines()] == [41, 42]
