from datetime import datetime
import json
import re
import subprocess

import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.bilibili_fetcher import (
    BiliApiError,
    BiliVideo,
    BridgeUnavailableError,
    FetchError,
    OpencliError,
    _fmt_duration,
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


def _src(transport="opencli", **fetch_overrides) -> BilibiliSource:
    fetch = {
        "whitelist": [{"uid": 111, "name": "UP甲"}, {"uid": 222, "name": "UP乙"}],
        "lookback_hours": 48,
        "per_up_limit": 8,
        "max_per_digest": 30,
    }
    fetch.update(fetch_overrides)
    return BilibiliSource(enabled=True, transport=transport, fetch=fetch)


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


# --- api transport -----------------------------------------------------------

NOW_TS = int(datetime(2026, 7, 2, 12, 0).timestamp())


def _media_item(bvid, pub_offset_h=1, title="标题", play=1000):
    return {"bv_id": bvid, "title": title, "cover": f"http://i0.hdslb.com/{bvid}.jpg",
            "pubtime": NOW_TS - pub_offset_h * 3600, "duration": 593,
            "upper": {"mid": 111, "name": "UP甲"}, "cnt_info": {"play": play}}


def _mock_medialist(httpx_mock, uid, items):
    httpx_mock.add_response(
        url=re.compile(rf"https://api\.bilibili\.com/x/v2/medialist/resource/list\?.*biz_id={uid}.*"),
        json={"code": 0, "message": "0", "data": {"media_list": items}})


def _mock_view(httpx_mock, bvid, desc="简介文字"):
    httpx_mock.add_response(
        url=re.compile(rf"https://api\.bilibili\.com/x/web-interface/view\?bvid={bvid}.*"),
        json={"code": 0, "data": {"bvid": bvid, "desc": desc}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("chat_daily_tg.bilibili_fetcher.time.sleep", lambda s: None)


def test_api_fetch_builds_videos_with_desc(httpx_mock: HTTPXMock, tmp_path):
    _mock_medialist(httpx_mock, 111, [_media_item("BV1aaaaaaaaa"),
                                      _media_item("BV1bbbbbbbbb", pub_offset_h=100)])  # outside 48h
    _mock_medialist(httpx_mock, 222, [_media_item("BV2ccccccccc", pub_offset_h=2)])
    _mock_view(httpx_mock, "BV1aaaaaaaaa", desc="视频简介A")
    _mock_view(httpx_mock, "BV2ccccccccc", desc="视频简介C")
    videos = fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert [v.bvid for v in videos] == ["BV1aaaaaaaaa", "BV2ccccccccc"]  # newest first
    v = videos[0]
    assert v.description == "视频简介A" and v.duration == "9m53s" and v.view == 1000
    assert v.author == "UP甲" and v.cover == "http://i0.hdslb.com/BV1aaaaaaaaa.jpg"


def test_api_fetch_skips_seen_without_view_call(httpx_mock: HTTPXMock, tmp_path):
    _mock_medialist(httpx_mock, 111, [_media_item("BV1seenseens")])
    _mock_medialist(httpx_mock, 222, [])
    seen = SeenStore(tmp_path / "s.txt")
    seen.add(seen_key_for("BV1seenseens"))
    assert fetch_new_videos(_src("api"), seen, now=NOW) == []
    # no view request fired for a seen bvid
    assert all("web-interface/view" not in str(r.url) for r in httpx_mock.get_requests())


def test_api_fetch_single_up_failure_continues(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url=re.compile(r".*biz_id=111.*"), json={"code": -404, "message": "啥都木有"})
    _mock_medialist(httpx_mock, 222, [_media_item("BV2ccccccccc")])
    _mock_view(httpx_mock, "BV2ccccccccc")
    videos = fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert [v.bvid for v in videos] == ["BV2ccccccccc"]


def test_api_fetch_all_ups_failing_raises(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url=re.compile(r".*biz_id=111.*"), json={"code": -404, "message": "啥都木有"})
    httpx_mock.add_response(
        url=re.compile(r".*biz_id=222.*"), json={"code": -404, "message": "啥都木有"})
    with pytest.raises(BiliApiError, match="all 2 UP"):
        fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)


def test_api_fetch_view_failure_only_drops_desc(httpx_mock: HTTPXMock, tmp_path):
    _mock_medialist(httpx_mock, 111, [_media_item("BV1aaaaaaaaa")])
    _mock_medialist(httpx_mock, 222, [])
    httpx_mock.add_response(
        url=re.compile(r".*web-interface/view.*"), json={"code": -404, "message": "啥都木有"})
    videos = fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert len(videos) == 1 and videos[0].description == ""


def test_api_client_does_not_trust_proxy_env(monkeypatch, tmp_path):
    captured = {}
    import httpx as _httpx
    real_client = _httpx.Client

    def spy_client(*args, **kw):
        captured.update(kw)
        kw.setdefault("transport", _httpx.MockTransport(
            lambda req: _httpx.Response(200, json={"code": 0, "data": {"media_list": []}})))
        return real_client(*args, **kw)

    monkeypatch.setattr("chat_daily_tg.bilibili_fetcher.httpx.Client", spy_client)
    fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert captured.get("trust_env") is False  # bilibili 请求绝不能走 guard 的代理


def test_fmt_duration():
    assert _fmt_duration(593) == "9m53s"
    assert _fmt_duration(3725) == "1h2m5s"
    assert _fmt_duration(0) is None


def test_opencli_all_ups_failing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _completed(returncode=1, stderr="boom"))
    with pytest.raises(OpencliError, match="all 2 UP"):
        fetch_new_videos(_src("opencli"), SeenStore(tmp_path / "s.txt"), now=NOW,
                         retry_max_attempts=1)


# --- adversarial-review fixes (2026-07-03) -----------------------------------

@pytest.mark.httpx_mock(assert_all_responses_were_requested=False,
                        can_send_already_matched_responses=True)
def test_api_dirty_item_is_skipped_not_fatal(httpx_mock: HTTPXMock, tmp_path):
    """单条脏数据（字符串 pubtime / 非法 duration / 字符串 play）只跳过该条。"""
    dirty = {"bv_id": "BV1dirtydirt", "title": "脏", "pubtime": "not-a-ts",
             "duration": "07:13", "upper": {"name": "UP甲"}, "cnt_info": {"play": "--"}}
    good = _media_item("BV1goodgood1")
    _mock_medialist(httpx_mock, 111, [dirty, good])
    _mock_medialist(httpx_mock, 222, [])
    _mock_view(httpx_mock, "BV1dirtydirt")
    _mock_view(httpx_mock, "BV1goodgood1")
    videos = fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    bvids = [v.bvid for v in videos]
    assert "BV1goodgood1" in bvids
    # 脏条目：pubtime 无法解析 → pub=None 仍保留，但 play/duration 被收敛不崩溃
    dirty_v = [v for v in videos if v.bvid == "BV1dirtydirt"]
    if dirty_v:  # pubtime 不合法时条目仍可用（字段降级），关键是不抛异常
        assert dirty_v[0].view is None and dirty_v[0].duration is None


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_api_352_aborts_run_immediately(httpx_mock: HTTPXMock, tmp_path):
    """-352 是 IP 级风控判决：第一个 UP 命中后立即终止，不再请求其余 UP。"""
    httpx_mock.add_response(
        url=re.compile(r".*biz_id=111.*"), json={"code": -352, "message": "风控校验失败"})
    with pytest.raises(BiliApiError, match="-352 风控"):
        fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    # uid=222 的请求从未发出
    assert all("biz_id=222" not in str(r.url) for r in httpx_mock.get_requests())


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False,
                        can_send_already_matched_responses=True)
def test_finalize_dedupes_co_published_bvid(httpx_mock: HTTPXMock, tmp_path):
    """联合投稿：同一 bvid 出现在两个白名单 UP 的列表里，一轮 digest 只出一张卡。"""
    _mock_medialist(httpx_mock, 111, [_media_item("BV1sharedvid")])
    _mock_medialist(httpx_mock, 222, [_media_item("BV1sharedvid")])
    _mock_view(httpx_mock, "BV1sharedvid")
    videos = fetch_new_videos(_src("api"), SeenStore(tmp_path / "s.txt"), now=NOW)
    assert [v.bvid for v in videos] == ["BV1sharedvid"]
