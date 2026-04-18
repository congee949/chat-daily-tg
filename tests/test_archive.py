from pathlib import Path
from wx_daily_tg.archive import safe_filename, prepare_archive_day


def test_safe_filename_replaces_slashes_and_colons():
    assert safe_filename("foo/bar:baz") == "foo_bar_baz"


def test_safe_filename_keeps_chinese_and_emoji():
    assert safe_filename("贝利VIP❤️") == "贝利VIP❤️"


def test_prepare_archive_day_creates_nested_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("wx_daily_tg.archive.ARCHIVE_DIR", tmp_path / "archive")
    p = prepare_archive_day("2026-04-17")
    assert p == tmp_path / "archive" / "2026" / "04" / "17"
    assert p.is_dir()
