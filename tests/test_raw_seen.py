from chat_daily_tg.raw_seen import SeenStore


def test_seen_add_contains_and_persist(tmp_path):
    p = tmp_path / "seen.txt"
    s = SeenStore(p)
    k = SeenStore.key("-100123", 7)
    assert k == "-100123:7"
    assert k not in s
    s.add(k)
    assert k in s
    # reload from disk → key persists
    s2 = SeenStore(p)
    assert k in s2


def test_seen_add_is_idempotent_no_dup_lines(tmp_path):
    p = tmp_path / "seen.txt"
    s = SeenStore(p)
    s.add("a:1")
    s.add("a:1")
    assert p.read_text().count("a:1") == 1


def test_seen_missing_file_starts_empty(tmp_path):
    s = SeenStore(tmp_path / "nope.txt")
    assert "x:1" not in s


def test_seen_max_msg_id_high_water_mark(tmp_path):
    s = SeenStore(tmp_path / "seen.txt")
    s.add("-100abc:10")
    s.add("-100abc:42")
    s.add("-100abc:7")
    s.add("-999other:99")
    assert s.max_msg_id("-100abc") == 42  # only this channel's ids
    assert s.max_msg_id("-999other") == 99
    assert s.max_msg_id("-100nope") == 0  # unknown channel → 0


def test_seen_max_msg_id_ignores_non_numeric_media_keys(tmp_path):
    path = tmp_path / "seen.txt"
    path.write_text(
        "youtube:video-id\n"
        "bilibili:BV1example\n"
        "-100abc:41\n"
        "malformed\n",
        encoding="utf-8",
    )

    store = SeenStore(path)

    assert "youtube:video-id" in store
    assert "bilibili:BV1example" in store
    assert store.max_msg_id("-100abc") == 41


def test_seen_max_msg_id_uses_index_without_iterating_seen_set(tmp_path):
    class NonIterableSet(set):
        def __iter__(self):
            raise AssertionError("max_msg_id must not scan the full seen set")

    store = SeenStore(tmp_path / "seen.txt")
    store.add("-100abc:10")
    store.add("-100abc:42")
    store._seen = NonIterableSet(store._seen)

    assert store.max_msg_id("-100abc") == 42
