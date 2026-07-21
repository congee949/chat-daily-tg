from datetime import datetime, timezone
import re

import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.config import YoutubeSource
from chat_daily_tg.raw_seen import SeenStore
import chat_daily_tg.youtube_fetcher as yt
from chat_daily_tg.youtube_fetcher import (
    YoutubeFetchError,
    _fmt_duration,
    fetch_new_videos,
    parse_iso_duration,
    seen_key_for,
)

CH_A = "UCaaaaaaaaaaaaaaaaaaaaaa"
CH_B = "UCbbbbbbbbbbbbbbbbbbbbbb"

NOW = datetime(2026, 7, 2, 12, 0)


def _src(**fetch_overrides) -> YoutubeSource:
    fetch = {
        "whitelist": [{"channel_id": CH_A, "name": "频道甲"},
                      {"channel_id": CH_B, "name": "频道乙"}],
        "lookback_hours": 48,
        "max_per_digest": 30,
    }
    fetch.update(fetch_overrides)
    return YoutubeSource(enabled=True, fetch=fetch)


def _published(local: datetime) -> str:
    """Naive LOCAL datetime → the UTC ISO form the RSS feed carries, so tests
    pass regardless of the machine's timezone."""
    return local.astimezone().astimezone(timezone.utc).isoformat()


def _entry(video_id: str, *, title="视频标题", author="频道甲",
           local_pub: datetime = datetime(2026, 7, 2, 8, 0),
           desc="简介", views="1234") -> str:
    return f"""
 <entry>
  <yt:videoId>{video_id}</yt:videoId>
  <title>{title}</title>
  <author><name>{author}</name></author>
  <published>{_published(local_pub)}</published>
  <media:group>
   <media:description>{desc}</media:description>
   <media:thumbnail url="https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" width="480" height="360"/>
   <media:community><media:statistics views="{views}"/></media:community>
  </media:group>
 </entry>"""


def _feed(*entries: str, title="频道甲") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns="http://www.w3.org/2005/Atom">\n'
        f" <title>{title}</title>{''.join(entries)}\n</feed>"
    )


def _mock_feed(httpx_mock, channel_id, xml):
    httpx_mock.add_response(
        url=re.compile(rf"https://www\.youtube\.com/feeds/videos\.xml\?channel_id={channel_id}"),
        content=xml.encode())


def _mock_videos_api(httpx_mock, durations: dict[str, str],
                     views: dict[str, int] | None = None):
    items = [{"id": vid,
              "contentDetails": {"duration": dur},
              "statistics": {"viewCount": str((views or {}).get(vid, 5000))}}
             for vid, dur in durations.items()]
    httpx_mock.add_response(
        url=re.compile(r"https://www\.googleapis\.com/youtube/v3/videos\?.*"),
        json={"items": items})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("chat_daily_tg.youtube_fetcher.time.sleep", lambda s: None)


# --- duration helpers --------------------------------------------------------

def test_parse_iso_duration():
    assert parse_iso_duration("PT9M53S") == 593
    assert parse_iso_duration("PT1H2M5S") == 3725
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("P1DT2H") == 93600
    assert parse_iso_duration("P0D") == 0          # live / premiere placeholder
    assert parse_iso_duration("garbage") is None
    assert parse_iso_duration("") is None


def test_fmt_duration():
    assert _fmt_duration(593) == "9m53s"
    assert _fmt_duration(3725) == "1h2m5s"
    assert _fmt_duration(0) is None
    assert _fmt_duration(None) is None


# --- fetch_new_videos --------------------------------------------------------

def test_fetch_filters_seen_lookback_and_enriches(httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("newvid00001", local_pub=datetime(2026, 7, 2, 8, 0)),
        _entry("oldvid00001", local_pub=datetime(2026, 6, 20, 8, 0)),   # outside 48h
        _entry("seenvid0001", local_pub=datetime(2026, 7, 2, 9, 0)),    # already seen
    ))
    _mock_feed(httpx_mock, CH_B, _feed(
        _entry("freshvid001", author="频道乙", local_pub=datetime(2026, 7, 1, 9, 0)),
        title="频道乙"))
    _mock_videos_api(httpx_mock, {"newvid00001": "PT9M53S", "freshvid001": "PT1H2M5S"},
                     views={"newvid00001": 45615})
    seen = SeenStore(tmp_path / "seen.txt")
    seen.add(seen_key_for("seenvid0001"))

    videos = fetch_new_videos(_src(), seen, api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["newvid00001", "freshvid001"]  # newest first
    v = videos[0]
    assert v.channel_id == CH_A and v.author == "频道甲"
    assert v.cover == "https://i.ytimg.com/vi/newvid00001/hqdefault.jpg"
    assert v.duration == "9m53s" and v.duration_seconds == 593
    assert v.view == 45615  # Data API viewCount wins over the RSS views attr
    assert v.url == "https://www.youtube.com/watch?v=newvid00001"
    assert v.seen_key == "youtube:newvid00001"
    # seen id 从未进入 enrichment 的 id= 参数
    api_reqs = [r for r in httpx_mock.get_requests() if "googleapis" in str(r.url)]
    assert api_reqs and "seenvid0001" not in str(api_reqs[0].url)


def test_shorts_and_live_placeholder_filtered_by_duration(httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("shortvid001"), _entry("border18001"), _entry("keeper00001"),
        _entry("livevid0001")))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    _mock_videos_api(httpx_mock, {
        "shortvid001": "PT59S",
        "border18001": "PT3M",     # exactly 180s → still a Short
        "keeper00001": "PT3M1S",   # 181s → kept
        "livevid0001": "P0D",      # live/premiere placeholder → dropped this round
    })
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["keeper00001"]


def test_both_enrichment_tiers_failing_ships_with_title_heuristic(
        httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("normalvid01"),
        _entry("shortsvid01", title="速看 #Shorts")))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    httpx_mock.add_response(
        url=re.compile(r"https://www\.googleapis\.com/.*"), status_code=500)
    for vid in ("normalvid01", "shortsvid01"):
        httpx_mock.add_response(
            url=re.compile(rf"https://www\.youtube\.com/watch\?v={vid}"),
            status_code=500)
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    # 投递优先于完美：时长未知仍推送；#shorts 标题启发式仍然生效
    assert [v.video_id for v in videos] == ["normalvid01"]
    assert videos[0].duration is None and videos[0].duration_seconds is None


def _watch_html(secs: int, views: int = 1000) -> bytes:
    return (f'<html>var ytInitialPlayerResponse = {{"videoDetails":'
            f'{{"lengthSeconds":"{secs}","viewCount":"{views}"}}}}</html>').encode()


def test_watch_page_fallback_when_api_blocked(httpx_mock: HTTPXMock, tmp_path):
    """2026-07-19 实况：GOOGLE_API_KEY 被控制台限制、youtube.v3 全 403——
    watch 页 lengthSeconds 兜底让 Shorts 过滤照常工作。"""
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("normalvid01"), _entry("shortsvid01")))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    httpx_mock.add_response(
        url=re.compile(r"https://www\.googleapis\.com/.*"), status_code=403)
    httpx_mock.add_response(
        url=re.compile(r"https://www\.youtube\.com/watch\?v=normalvid01"),
        content=_watch_html(613, views=45615))
    httpx_mock.add_response(
        url=re.compile(r"https://www\.youtube\.com/watch\?v=shortsvid01"),
        content=_watch_html(45))
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["normalvid01"]
    assert videos[0].duration == "10m13s" and videos[0].duration_seconds == 613
    assert videos[0].view == 45615


def test_no_api_key_goes_straight_to_watch_page(httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(_entry("normalvid01")))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    httpx_mock.add_response(
        url=re.compile(r"https://www\.youtube\.com/watch\?v=normalvid01"),
        content=_watch_html(613))
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key=None, now=NOW)
    assert [v.video_id for v in videos] == ["normalvid01"]
    assert videos[0].duration_seconds == 613
    assert all("googleapis" not in str(r.url) for r in httpx_mock.get_requests())


def test_single_channel_failure_does_not_kill_run(httpx_mock: HTTPXMock, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(yt, "_FEED_RETRY_BACKOFF_SECONDS", 0)
    # 500 is retryable (YouTube flake) — must persist across all attempts.
    httpx_mock.add_response(
        url=re.compile(rf".*channel_id={CH_A}.*"), status_code=500,
        is_reusable=True)
    _mock_feed(httpx_mock, CH_B, _feed(
        _entry("freshvid001", author="频道乙"), title="频道乙"))
    _mock_videos_api(httpx_mock, {"freshvid001": "PT10M"})
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["freshvid001"]


def test_flaky_feed_recovers_on_retry(httpx_mock: HTTPXMock, tmp_path, monkeypatch):
    """The 2025-12+ YouTube flake: first attempt 404s, retry succeeds — the
    channel must NOT count as failed and its videos must ship."""
    monkeypatch.setattr(yt, "_FEED_RETRY_BACKOFF_SECONDS", 0)
    httpx_mock.add_response(
        url=re.compile(rf".*channel_id={CH_A}.*"), status_code=404)
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("retryvid001", author="频道甲"), title="频道甲"))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    _mock_videos_api(httpx_mock, {"retryvid001": "PT10M"})
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["retryvid001"]


def test_all_channels_failing_raises(httpx_mock: HTTPXMock, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "_FEED_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(yt, "_FEED_WAVE_BACKOFF_SECONDS", 0)
    # Retries exhausted on every channel across all waves (is_reusable covers
    # per-channel attempts + the delayed second wave).
    httpx_mock.add_response(
        url=re.compile(r"https://www\.youtube\.com/feeds/.*"), status_code=500,
        is_reusable=True)
    with pytest.raises(YoutubeFetchError, match="all 2 channel"):
        fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)


def test_all_fail_first_wave_recovers_on_second(httpx_mock: HTTPXMock, tmp_path,
                                                monkeypatch):
    """Multi-minute flake storm: wave-1 exhausts every channel, wave-2 recovers
    after the inter-wave pause — must not raise and must ship the video."""
    monkeypatch.setattr(yt, "_FEED_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(yt, "_FEED_WAVE_BACKOFF_SECONDS", 0)
    # CH_A: 3 failed attempts (wave 1) then success (wave 2).
    for _ in range(yt._FEED_ATTEMPTS):
        httpx_mock.add_response(
            url=re.compile(rf".*channel_id={CH_A}.*"), status_code=404)
    _mock_feed(httpx_mock, CH_A, _feed(
        _entry("wave2vid001", author="频道甲"), title="频道甲"))
    # CH_B: same pattern, empty feed on recovery.
    for _ in range(yt._FEED_ATTEMPTS):
        httpx_mock.add_response(
            url=re.compile(rf".*channel_id={CH_B}.*"), status_code=500)
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    _mock_videos_api(httpx_mock, {"wave2vid001": "PT10M"})
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["wave2vid001"]


def test_blacklist_and_cap(httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(
        *[_entry(f"vidnumber{i:02d}", local_pub=datetime(2026, 7, 2, i))
          for i in range(3)]))
    _mock_videos_api(httpx_mock, {f"vidnumber{i:02d}": "PT10M" for i in range(3)})
    src = _src(blacklist=[{"channel_id": CH_B}], max_per_digest=2)
    videos = fetch_new_videos(src, SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert len(videos) == 2  # capped, and CH_B never fetched
    assert all(v.channel_id == CH_A for v in videos)
    assert all(f"channel_id={CH_B}" not in str(r.url)
               for r in httpx_mock.get_requests())


def test_dirty_entry_is_skipped_not_fatal(httpx_mock: HTTPXMock, tmp_path):
    bad = "<entry><yt:videoId>tooshort</yt:videoId><title>坏条目</title></entry>"
    _mock_feed(httpx_mock, CH_A, _feed(bad, _entry("normalvid01")))
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    _mock_videos_api(httpx_mock, {"normalvid01": "PT10M"})
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["normalvid01"]


def test_unparseable_published_is_kept(httpx_mock: HTTPXMock, tmp_path):
    xml = _feed(_entry("normalvid01")).replace(
        _published(datetime(2026, 7, 2, 8, 0)), "not-a-date")
    _mock_feed(httpx_mock, CH_A, xml)
    _mock_feed(httpx_mock, CH_B, _feed(title="频道乙"))
    _mock_videos_api(httpx_mock, {"normalvid01": "PT10M"})
    videos = fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    assert [v.video_id for v in videos] == ["normalvid01"]
    assert videos[0].publish_time is None


def test_per_channel_topic_passthrough(httpx_mock: HTTPXMock, tmp_path):
    _mock_feed(httpx_mock, CH_A, _feed(_entry("normalvid01")))
    _mock_feed(httpx_mock, CH_B, _feed(
        _entry("englishvid1", author="频道乙"), title="频道乙"))
    _mock_videos_api(httpx_mock, {"normalvid01": "PT10M", "englishvid1": "PT10M"})
    src = _src(whitelist=[{"channel_id": CH_A},
                          {"channel_id": CH_B, "topic": "english"}])
    videos = fetch_new_videos(src, SeenStore(tmp_path / "s.txt"), api_key="K", now=NOW)
    topics = {v.video_id: v.topic for v in videos}
    assert topics == {"normalvid01": None, "englishvid1": "english"}


def test_client_transport_carries_env_proxy(monkeypatch, tmp_path):
    """与 B 站相反：YouTube 请求必须吃到 wrapper 的 HTTP(S)_PROXY。httpx 传
    显式 transport 会绕过 trust_env 的代理挂载（2026-07-19 部署干跑实锤：
    静默直连 → 大陆出口 TLS 握手全灭），所以代理必须显式装进 transport。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8888")
    captured = {}
    import httpx as _httpx

    def fake_transport(*args, **kw):
        captured.update(kw)
        return _httpx.MockTransport(
            lambda req: _httpx.Response(200, content=_feed(title="x").encode()))

    monkeypatch.setattr("chat_daily_tg.youtube_fetcher.httpx.HTTPTransport", fake_transport)
    fetch_new_videos(_src(), SeenStore(tmp_path / "s.txt"), api_key=None, now=NOW)
    assert captured.get("proxy") == "http://proxy.test:8888"


def test_proxy_from_env_precedence(monkeypatch):
    from chat_daily_tg.youtube_fetcher import _proxy_from_env
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert _proxy_from_env() is None
    monkeypatch.setenv("http_proxy", "http://low.test:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://high.test:2")
    assert _proxy_from_env() == "http://high.test:2"
