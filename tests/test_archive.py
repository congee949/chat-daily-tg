from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.archive import safe_filename, prepare_archive_day, cleanup_old_media


def test_safe_filename_replaces_slashes_and_colons():
    assert safe_filename("foo/bar:baz") == "foo_bar_baz"


def test_safe_filename_keeps_chinese_and_emoji():
    assert safe_filename("示例群❤️") == "示例群❤️"


def test_prepare_archive_day_creates_nested_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("chat_daily_tg.paths.ARCHIVE_DIR", tmp_path / "archive")
    p = prepare_archive_day("2026-04-17")
    assert p == tmp_path / "archive" / "2026" / "04" / "17"
    assert p.is_dir()


def _day_dir(archive_root: Path, day: date) -> Path:
    p = archive_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_cleanup_old_media_removes_media_dirs_past_retention(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("chat_daily_tg.paths.ARCHIVE_DIR", tmp_path / "archive")
    old_day = _day_dir(tmp_path / "archive", date.today() - timedelta(days=20))
    (old_day / "tg_media" / "chat").mkdir(parents=True)
    (old_day / "tg_media" / "chat" / "1.jpg").write_bytes(b"x" * 100)
    (old_day / "wx_media" / "group").mkdir(parents=True)
    (old_day / "wx_media" / "group" / "2.jpg").write_bytes(b"x" * 50)
    (old_day / "summary.md").write_text("keep me", encoding="utf-8")

    removed, freed = cleanup_old_media(retention_days=14)

    assert removed == 2
    assert freed == 150
    assert not (old_day / "tg_media").exists()
    assert not (old_day / "wx_media").exists()
    assert (old_day / "summary.md").read_text(encoding="utf-8") == "keep me"


def test_cleanup_old_media_keeps_recent_day_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("chat_daily_tg.paths.ARCHIVE_DIR", tmp_path / "archive")
    recent_day = _day_dir(tmp_path / "archive", date.today() - timedelta(days=3))
    (recent_day / "tg_media" / "chat").mkdir(parents=True)
    (recent_day / "tg_media" / "chat" / "1.jpg").write_bytes(b"x")

    removed, freed = cleanup_old_media(retention_days=14)

    assert removed == 0
    assert freed == 0
    assert (recent_day / "tg_media" / "chat" / "1.jpg").exists()


def test_cleanup_old_media_missing_archive_dir_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("chat_daily_tg.paths.ARCHIVE_DIR", tmp_path / "does-not-exist")
    assert cleanup_old_media(retention_days=14) == (0, 0)
