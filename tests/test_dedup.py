from wx_daily_tg.dedup import find_cross_group_dupes, DedupKey


def test_same_url_in_two_groups_is_dupe():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00",
         "content": "看这个 https://example.com/offer"},
        {"group": "G2", "sender": "A", "time": "10:05",
         "content": "https://example.com/offer 源头"},
    ]
    groups = find_cross_group_dupes(msgs)
    assert len(groups) == 1
    assert groups[0].key == DedupKey(kind="url", value="https://example.com/offer")
    assert {m["group"] for m in groups[0].messages} == {"G1", "G2"}


def test_different_urls_no_dupe():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00",
         "content": "https://a.example"},
        {"group": "G2", "sender": "B", "time": "10:05",
         "content": "https://b.example"},
    ]
    assert find_cross_group_dupes(msgs) == []


def test_long_text_content_hash_matches():
    long = "X" * 100
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00", "content": long},
        {"group": "G2", "sender": "A", "time": "10:01", "content": long},
    ]
    groups = find_cross_group_dupes(msgs)
    assert len(groups) == 1
    assert groups[0].key.kind == "content_hash"


def test_short_text_not_deduped():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00", "content": "好的"},
        {"group": "G2", "sender": "B", "time": "10:01", "content": "好的"},
    ]
    assert find_cross_group_dupes(msgs) == []


def test_message_with_multiple_fingerprints_appears_in_each_group():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00",
         "content": "Visit https://x.com and call 13812345678"},
        {"group": "G2", "sender": "B", "time": "10:05",
         "content": "https://x.com is cool"},
        {"group": "G3", "sender": "C", "time": "10:10",
         "content": "Ring 13812345678 please"},
    ]
    groups = find_cross_group_dupes(msgs)
    keys = {g.key.kind for g in groups}
    assert "url" in keys
    assert "phone" in keys
    # The G1 message should appear in BOTH the url group (with G2) AND the phone group (with G3)
    g1_msg = msgs[0]
    url_group = next(g for g in groups if g.key.kind == "url")
    phone_group = next(g for g in groups if g.key.kind == "phone")
    assert g1_msg in url_group.messages
    assert g1_msg in phone_group.messages
