from datetime import datetime
import json
import subprocess

import pytest

from chat_daily_tg.bilibili_fetcher import (
    BiliVideo,
    BridgeUnavailableError,
    OpencliError,
    _parse_detail,
    _parse_publish_time,
    fetch_new_videos,
    probe_bridge,
    run_opencli,
    seen_key_for,
)
from chat_daily_tg.config import BilibiliSource
from chat_daily_tg.raw_seen import SeenStore


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _src(**fetch_overrides) -> BilibiliSource:
    fetch = {
        "whitelist": [{"uid": 111, "name": "UP甲"}, {"uid": 222, "name": "UP乙"}],
        "lookback_hours": 48,
        "per_up_limit": 8,
        "max_per_digest": 30,
    }
    fetch.update(fetch_overrides)
    return BilibiliSource(enabled=True, fetch=fetch)


# --- run_opencli -------------------------------------------------------------

def test_run_opencli_parses_json_and_forces_background_window(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed(stdout='[{"a": 1}]')

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_opencli(["user-videos", "111"])
    assert out == [{"a": 1}]
    assert calls[0][:2] == ["opencli", "bilibili"]
    assert "--window" in calls[0] and "background" in calls[0]
    assert "-f" in calls[0] and "json" in calls[0]


def test_run_opencli_profile_flag_precedes_service(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _completed(stdout="[]"))
    run_opencli(["feed"], profile="alt")
    assert calls[0][:4] == ["opencli", "--profile", "alt", "bilibili"]


def test_run_opencli_retries_then_raises(monkeypatch):
    attempts = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: attempts.append(1) or _completed(returncode=1, stderr="boom"))
    monkeypatch.setattr("chat_daily_tg.bilibili_fetcher.time.sleep", lambda s: None)
    with pytest.raises(OpencliError, match="boom"):
        run_opencli(["video", "BVx"], retry_max_attempts=3)
    assert len(attempts) == 3


def test_run_opencli_bad_json_is_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(stdout="not json"))
    monkeypatch.setattr("chat_daily_tg.bilibili_fetcher.time.sleep", lambda s: None)
    with pytest.raises(OpencliError, match="bad JSON"):
        run_opencli(["feed"], retry_max_attempts=2)


# --- probe_bridge ------------------------------------------------------------

def test_probe_bridge_ok(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _completed(stdout="[OK] Connectivity: connected in 0.1s"))
    probe_bridge()  # no raise


def test_probe_bridge_down(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _completed(returncode=1, stderr="daemon not running"))
    with pytest.raises(BridgeUnavailableError):
        probe_bridge()


def test_probe_bridge_no_connectivity_line(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _completed(stdout="[FAIL] Extension: disconnected"))
    with pytest.raises(BridgeUnavailableError):
        probe_bridge()


# --- parsing helpers ---------------------------------------------------------

def test_parse_detail_field_value_rows():
    rows = [{"field": "bvid", "value": "BVx"}, {"field": "title", "value": "标题"}]
    assert _parse_detail(rows) == {"bvid": "BVx", "title": "标题"}


def test_parse_publish_time_formats():
    assert _parse_publish_time("2026-06-29 01:00") == datetime(2026, 6, 29, 1, 0)
    assert _parse_publish_time("2026-07-01") == datetime(2026, 7, 1)
    assert _parse_publish_time("8分钟前") is None


# --- fetch_new_videos --------------------------------------------------------

def _fake_opencli_factory(user_videos: dict[str, list], details: dict[str, list]):
    """Return a fake subprocess.run understanding user-videos/video commands."""
    def fake_run(cmd, **kw):
        if "user-videos" in cmd:
            uid = cmd[cmd.index("user-videos") + 1]
            return _completed(stdout=json.dumps(user_videos.get(uid, [])))
        if "video" in cmd:
            bvid = cmd[cmd.index("video") + 1]
            return _completed(stdout=json.dumps(details[bvid]))
        if "doctor" in cmd:
            return _completed(stdout="[OK] Connectivity: connected")
        raise AssertionError(f"unexpected cmd: {cmd}")
    return fake_run


def _detail_rows(bvid, title="标题", author="UP甲 (mid: 111)",
                 publish="2026-07-02 08:00", thumb="http://cover/x.jpg"):
    return [
        {"field": "bvid", "value": bvid},
        {"field": "title", "value": title},
        {"field": "author", "value": author},
        {"field": "publish_time", "value": publish},
        {"field": "duration", "value": "8m4s (484s)"},
        {"field": "view", "value": "45615"},
        {"field": "thumbnail", "value": thumb},
        {"field": "description", "value": "简介"},
    ]


NOW = datetime(2026, 7, 2, 12, 0)


def test_fetch_filters_seen_lookback_and_enriches(monkeypatch, tmp_path):
    user_videos = {
        "111": [
            {"url": "https://www.bilibili.com/video/BV1newnewnew", "date": "2026-07-02"},
            {"url": "https://www.bilibili.com/video/BV1oldoldold", "date": "2026-06-20"},  # outside 48h
            {"url": "https://www.bilibili.com/video/BV1seenseens", "date": "2026-07-02"},  # already seen
        ],
        "222": [
            {"url": "https://www.bilibili.com/video/BV2freshfres", "date": "2026-07-01"},
        ],
    }
    details = {
        "BV1newnewnew": _detail_rows("BV1newnewnew", publish="2026-07-02 08:00"),
        "BV2freshfres": _detail_rows("BV2freshfres", author="UP乙 (mid: 222)",
                                     publish="2026-07-01 09:00"),
    }
    monkeypatch.setattr(subprocess, "run", _fake_opencli_factory(user_videos, details))
    seen = SeenStore(tmp_path / "seen.txt")
    seen.add(seen_key_for("BV1seenseens"))

    videos = fetch_new_videos(_src(), seen, now=NOW)
    assert [v.bvid for v in videos] == ["BV1newnewnew", "BV2freshfres"]  # newest first
    v = videos[0]
    assert v.uid == 111 and v.author == "UP甲"  # mid suffix stripped
    assert v.cover == "http://cover/x.jpg" and v.duration == "8m4s" and v.view == 45615
    assert v.seen_key == "bilibili:BV1newnewnew"


def test_fetch_detail_publish_time_refines_day_granular_date(monkeypatch, tmp_path):
    # date says today (in window) but precise publish_time is beyond lookback
    user_videos = {"111": [{"url": "https://b.com/video/BV1borderlin", "date": "2026-06-30"}],
                   "222": []}
    details = {"BV1borderlin": _detail_rows("BV1borderlin", publish="2026-06-30 08:00")}
    monkeypatch.setattr(subprocess, "run", _fake_opencli_factory(user_videos, details))
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert videos == []  # 2026-06-30 08:00 < cutoff 2026-06-30 12:00


def test_fetch_single_up_failure_does_not_kill_run(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        if "user-videos" in cmd and "111" in cmd:
            return _completed(returncode=1, stderr="boom")
        if "user-videos" in cmd:
            return _completed(stdout=json.dumps(
                [{"url": "https://b.com/video/BV2freshfres", "date": "2026-07-02"}]))
        if "video" in cmd:
            return _completed(stdout=json.dumps(
                _detail_rows("BV2freshfres", author="UP乙 (mid: 222)")))
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("chat_daily_tg.bilibili_fetcher.time.sleep", lambda s: None)
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), now=NOW,
                              retry_max_attempts=1)
    assert [v.bvid for v in videos] == ["BV2freshfres"]


def test_fetch_blacklist_and_cap(monkeypatch, tmp_path):
    user_videos = {
        "111": [{"url": f"https://b.com/video/BV1aaaaaa{i:03d}", "date": "2026-07-02"}
                for i in range(3)],
        "222": [{"url": "https://b.com/video/BV2xxxxxxxxx", "date": "2026-07-02"}],
    }
    details = {f"BV1aaaaaa{i:03d}": _detail_rows(f"BV1aaaaaa{i:03d}",
                                                 publish=f"2026-07-02 0{i}:00")
               for i in range(3)}
    monkeypatch.setattr(subprocess, "run", _fake_opencli_factory(user_videos, details))
    src = _src(blacklist=[{"uid": 222}], max_per_digest=2)
    videos = fetch_new_videos(src, SeenStore(tmp_path / "s.txt"), now=NOW)
    assert len(videos) == 2  # capped, and uid 222 never fetched
    assert all(v.uid == 111 for v in videos)
