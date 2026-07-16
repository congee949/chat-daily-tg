import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from chat_daily_tg.content_seen import (
    ContentSeenStore,
    XMonitorIndex,
    canonical_urls,
    canonicalize_url,
    check_duplicate,
    extract_urls,
    fingerprints_for,
    is_bare_link_post,
    text_fingerprint,
    text_key,
    tweet_keys_from_urls,
    url_key,
)

# --------------------------------------------------------------------------- #
# acceptance fixture — the 2026-06-27 corpus pair (科技圈在花 42209 ↔ yihong 13645)

TEXT_A = (
    "**北京朝阳一轻型航空器撞楼致 1 死 13 伤**\n\n"
    "6 月 26 日 17 时 55 分，一架单发双座轻型运动航空器在北京朝阳区东三环附近飞行时碰撞一高层建筑。"
    "机上仅驾驶员一人，已当场死亡，现场另有 13 人受伤。\n\n"
    "目前伤者正在全力救治中，事故原因尚不清楚，相关情况已由主管部门进一步调查。\n\n"
    "[北京朝阳](https://mp.weixin.qq.com/s/pJ-uTGYI1p5XPx0lyitAHw)\n\n"
    "🌸 [在花频道](http://t.me/ZaiHuaPd) · [茶馆水群](https://t.me/zaihuachat) · "
    "[投稿通道](http://t.me/ZaiHuabot)"
)
TEXT_B = (
    "”北京朝阳“微信公众号27日通报，“小飞机撞击中国尊事件”导致飞机驾驶员死亡，另有13人受伤。"
    "机上仅驾驶员1人。\n"
    "（[北京朝阳](https://mp.weixin.qq.com/s/pJ-uTGYI1p5XPx0lyitAHw)）"
)
WEIXIN_URL = "https://mp.weixin.qq.com/s/pJ-uTGYI1p5XPx0lyitAHw"


def test_corpus_pair_shares_exactly_the_weixin_url():
    shared = canonical_urls(TEXT_A) & canonical_urls(TEXT_B)
    assert shared == {WEIXIN_URL}


def test_corpus_pair_neither_is_bare_link():
    # Both carry substantive prose — URL identity alone must never skip them.
    assert not is_bare_link_post(TEXT_A)
    assert not is_bare_link_post(TEXT_B)


def test_corpus_pair_text_fingerprints_differ():
    fp_a = text_fingerprint(TEXT_A)
    fp_b = text_fingerprint(TEXT_B)
    assert fp_a is not None and fp_b is not None
    assert fp_a != fp_b


# --------------------------------------------------------------------------- #
# URL extraction / canonicalization

@pytest.mark.parametrize("url", [
    "https://fxtwitter.com/user/status/123",
    "https://vxtwitter.com/user/status/123",
    "https://fixupx.com/user/status/123",
    "https://fixvx.com/user/status/123",
    "https://mobile.twitter.com/user/status/123",
    "https://twitter.com/user/status/123",
    "https://x.com/user/status/123",
    "https://X.com/user/status/123",              # host case-insensitive
    "https://www.twitter.com/user/status/123",    # www. stripped before host match
    "https://x.com/i/status/123",                 # author-less, dominant bare-link form
    "https://x.com/i/web/status/123",
    "https://twitter.com/user/statuses/123",      # legacy /statuses/
    "https://x.com/user/status/123/photo/1",      # media suffix stripped
    "https://x.com/user/status/123/video/2",
    "https://x.com/user/status/123?s=20&t=AbCdEf",  # share junk dropped
])
def test_tweet_urls_canonicalize_to_x_status(url):
    assert canonicalize_url(url) == "x.com/status/123"


def test_article_urls_both_forms_canonicalize_to_i_article():
    assert canonicalize_url("https://x.com/i/article/abc-123") == "x.com/i/article/abc-123"
    assert canonicalize_url("https://x.com/someuser/articles/abc-123") == "x.com/i/article/abc-123"


def test_tweet_keys_from_urls_maps_to_t_and_a_keys():
    keys = tweet_keys_from_urls({
        "x.com/status/123",
        "x.com/i/article/abc-123",
        "https://example.com/other",
    })
    assert keys == {"t:123", "a:abc-123"}


def test_tco_link_canonicalizes_generically_without_tweet_key():
    c = canonicalize_url("https://t.co/AbC123xyz")
    assert c == "https://t.co/AbC123xyz"
    assert tweet_keys_from_urls({c}) == set()


def test_bilibili_share_source_junk_dropped():
    c = canonicalize_url(
        "https://www.bilibili.com/video/BV1xx411c7mD"
        "?share_source=copy_web&share_medium=iphone&vd_source=deadbeef"
    )
    assert c == "bilibili.com/video/BV1xx411c7mD"


def test_wiki_balanced_parens_survive():
    urls = extract_urls("条目 https://en.wikipedia.org/wiki/Python_(programming_language) 不错")
    assert urls == ["https://en.wikipedia.org/wiki/Python_(programming_language)"]


def test_wiki_unbalanced_trailing_paren_from_prose_stripped():
    urls = extract_urls("(see https://en.wikipedia.org/wiki/Python_(programming_language)) here")
    assert urls == ["https://en.wikipedia.org/wiki/Python_(programming_language)"]


def test_url_glued_to_cjk_prose_terminates_at_ideograph():
    urls = extract_urls("详情见https://example.com/news/123这篇报道")
    assert urls == ["https://example.com/news/123"]


def test_markdown_link_never_leaks_closing_paren():
    urls = extract_urls("点开([链接](https://example.com/x))看看")
    assert urls == ["https://example.com/x"]


def test_generic_host_utm_stripped_but_page_kept():
    c = canonicalize_url("https://example.com/list?page=2&utm_source=tg&utm_campaign=x")
    assert c == "https://example.com/list?page=2"


def test_host_case_insensitive_and_www_stripped():
    assert canonicalize_url("HTTPS://WWW.Example.COM/Path") == "https://example.com/Path"


# --------------------------------------------------------------------------- #
# bare-link classifier

def test_pure_url_is_bare():
    assert is_bare_link_post("https://x.com/i/status/1234567890")


def test_url_plus_share_tag_is_bare():
    assert is_bare_link_post("https://x.com/i/status/1234567890 #share")


def test_url_plus_chinese_editorial_remark_is_not_bare():
    # 21 substantive Chinese code points of commentary — a deliberate remark.
    text = "https://x.com/i/status/1234567890 这条消息值得认真读一读因为信息量真的非常大"
    assert not is_bare_link_post(text)


def test_markdown_link_with_substantive_label_is_not_bare():
    assert not is_bare_link_post("[这是一个非常有价值的深度长文推荐](https://example.com/article)")


def test_empty_and_whitespace_text_never_bare():
    assert not is_bare_link_post("")
    assert not is_bare_link_post("   \n  ")


def test_text_without_url_is_not_bare():
    assert not is_bare_link_post("纯文本没有任何链接")


# --------------------------------------------------------------------------- #
# text_fingerprint

_FP_BASE = "这条新闻的内容足够长可以生成指纹用于测试目的完全一致"  # 26 substantive chars


def test_identical_text_same_fingerprint():
    assert text_fingerprint(_FP_BASE) == text_fingerprint(_FP_BASE)
    assert text_fingerprint(_FP_BASE) is not None


def test_urls_and_trackers_excluded_from_fingerprint():
    fp_a = text_fingerprint(_FP_BASE + "\nhttps://example.com/a?utm_source=x")
    fp_b = text_fingerprint(_FP_BASE + "\nhttps://other.org/b?utm_campaign=y")
    assert fp_a is not None
    assert fp_a == fp_b == text_fingerprint(_FP_BASE)


def test_short_post_never_fingerprints():
    assert text_fingerprint("哭了") is None


def test_fullwidth_and_case_fold_via_nfkc():
    fw = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"  # 26 fullwidth letters
    assert text_fingerprint(fw) == text_fingerprint("abcdefghijklmnopqrstuvwxyz")


def test_punctuation_only_differences_collapse():
    with_punct = "今天天气很好，我们一起去公园散步吧！然后再去喝一杯咖啡。"
    without = "今天天气很好我们一起去公园散步吧然后再去喝一杯咖啡"
    assert text_fingerprint(with_punct) == text_fingerprint(without)
    assert text_fingerprint(without) is not None


# --------------------------------------------------------------------------- #
# ContentSeenStore

def test_store_register_lookup_roundtrip(tmp_path):
    store = ContentSeenStore(tmp_path / "content_seen.db")
    store.register(["text:abc", "url:def"], chat_id="-100123", msg_id=42, channel="在花")
    hit = store.lookup(["text:abc"])
    assert hit is not None
    assert hit.fingerprint == "text:abc"
    assert hit.chat_id == "-100123"
    assert hit.msg_id == 42
    assert hit.channel == "在花"
    assert hit.sent_at  # ISO timestamp recorded
    assert store.lookup(["url:def"]) is not None
    store.close()


def test_store_insert_or_ignore_keeps_original_owner(tmp_path):
    store = ContentSeenStore(tmp_path / "content_seen.db")
    store.register(["text:abc"], chat_id="-100first", msg_id=1, channel="first")
    store.register(["text:abc"], chat_id="-100second", msg_id=2, channel="second")
    hit = store.lookup(["text:abc"])
    assert hit.chat_id == "-100first"
    assert hit.msg_id == 1
    assert hit.channel == "first"
    store.close()


def test_store_prunes_rows_outside_window_on_open(tmp_path):
    db = tmp_path / "content_seen.db"
    store = ContentSeenStore(db, window_days=14)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    with store._conn:
        store._conn.execute(
            "INSERT INTO content_seen VALUES (?,?,?,?,?)",
            ("text:old", "-100", 1, "ch", old),
        )
    assert store.lookup(["text:old"]) is not None  # visible until next open
    store.close()

    reopened = ContentSeenStore(db, window_days=14)
    assert reopened.lookup(["text:old"]) is None
    reopened.close()


def test_store_lookup_empty_list_is_none(tmp_path):
    store = ContentSeenStore(tmp_path / "content_seen.db")
    assert store.lookup([]) is None
    store.close()


def test_fingerprints_for_returns_text_and_url_keys():
    text = _FP_BASE + "\nhttps://example.com/a"
    fps = fingerprints_for(text)
    assert fps == [
        text_key(text_fingerprint(text)),
        url_key("https://example.com/a"),
    ]


# --------------------------------------------------------------------------- #
# XMonitorIndex

def _fresh_ts(days_ago: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _write_index(path, entries: dict) -> None:
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")


def test_xmon_missing_file_loads_empty(tmp_path):
    idx = XMonitorIndex(tmp_path / "nope.json")
    assert idx.entries == {}
    assert idx.stale is False
    assert idx.lookup({"t:123"}) is None


def test_xmon_malformed_json_loads_empty(tmp_path):
    p = tmp_path / "pushed_index.json"
    p.write_text("not json{{{", encoding="utf-8")
    idx = XMonitorIndex(p)
    assert idx.entries == {}
    assert idx.lookup({"t:123"}) is None


def test_xmon_entries_wrong_type_loads_empty(tmp_path):
    p = tmp_path / "pushed_index.json"
    p.write_text(json.dumps({"entries": [1, 2, 3]}), encoding="utf-8")
    idx = XMonitorIndex(p)
    assert idx.entries == {}


def test_xmon_per_entry_ttl_reapplied_at_load(tmp_path):
    p = tmp_path / "pushed_index.json"
    _write_index(p, {
        "t:old": {"ts": _fresh_ts(days_ago=20), "by": "m"},
        "t:new": {"ts": _fresh_ts(days_ago=1), "by": "m"},
    })
    idx = XMonitorIndex(p, ttl_days=14)
    assert "t:old" not in idx.entries
    assert "t:new" in idx.entries


def test_xmon_stale_file_counts_as_absent(tmp_path):
    p = tmp_path / "pushed_index.json"
    _write_index(p, {"t:123": {"ts": _fresh_ts(), "by": "m"}})
    two_days_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(p, (two_days_ago, two_days_ago))
    idx = XMonitorIndex(p, max_age_hours=24)
    assert idx.stale is True
    assert idx.entries == {}
    assert idx.lookup({"t:123"}) is None


def test_xmon_assumed_entry_skipped_by_lookup(tmp_path):
    p = tmp_path / "pushed_index.json"
    _write_index(p, {"t:123": {"ts": _fresh_ts(), "by": "m", "assumed": True}})
    idx = XMonitorIndex(p)
    assert "t:123" in idx.entries  # loaded, but not delivered evidence
    assert idx.lookup({"t:123"}) is None


def test_xmon_normal_hit_returns_key_and_entry(tmp_path):
    p = tmp_path / "pushed_index.json"
    ts = _fresh_ts()
    _write_index(p, {"t:123": {"ts": ts, "by": "x_monitor", "ok": True}})
    idx = XMonitorIndex(p)
    hit = idx.lookup({"t:123", "t:999"})
    assert hit is not None
    key, entry = hit
    assert key == "t:123"
    assert entry["by"] == "x_monitor"
    assert entry["ts"] == ts


# --------------------------------------------------------------------------- #
# check_duplicate end-to-end

@pytest.fixture
def store(tmp_path):
    s = ContentSeenStore(tmp_path / "content_seen.db")
    yield s
    s.close()


def test_check_duplicate_text_hit_skips(store):
    store.register(fingerprints_for(TEXT_A), chat_id="-100zaihua", msg_id=42209, channel="在花")
    d = check_duplicate(TEXT_A, store=store)
    assert d.skip is True
    assert d.reason == "text"
    assert d.detail["matched_chat_id"] == "-100zaihua"
    assert d.detail["matched_msg_id"] == 42209


def test_check_duplicate_bare_link_url_hit_skips(store):
    store.register([url_key("x.com/status/123")], chat_id="-100a", msg_id=7)
    d = check_duplicate("https://fxtwitter.com/foo/status/123", store=store)
    assert d.skip is True
    assert d.reason == "url"
    assert d.detail["matched_msg_id"] == 7


def test_check_duplicate_same_url_with_commentary_delivers(store):
    store.register([url_key("x.com/status/123")], chat_id="-100a", msg_id=7)
    text = "这条推的观点值得展开说说因为它触及了行业核心矛盾 https://x.com/foo/status/123"
    d = check_duplicate(text, store=store)
    assert d.skip is False


def test_check_duplicate_xmon_hit_skips_with_detail(store, tmp_path):
    p = tmp_path / "pushed_index.json"
    ts = _fresh_ts()
    _write_index(p, {"t:123": {"ts": ts, "by": "x_monitor"}})
    xmon = XMonitorIndex(p)
    d = check_duplicate("https://x.com/i/status/123", store=store, xmon=xmon)
    assert d.skip is True
    assert d.reason == "xmon"
    assert d.detail == {"matched_key": "t:123", "matched_by": "x_monitor", "matched_ts": ts}


def test_check_duplicate_without_xmon_no_gate(store):
    d = check_duplicate("https://x.com/i/status/123", store=store, xmon=None)
    assert d.skip is False


def test_check_duplicate_no_urls_short_text_delivers(store):
    d = check_duplicate("哭了", store=store)
    assert d.skip is False
    assert d.reason == ""
