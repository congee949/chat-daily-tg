from pathlib import Path
from wx_daily_tg.db import PermanentDB, PermanentEntry
from wx_daily_tg.permanent_md import regenerate_permanent_md


def test_regenerate_groups_by_category(tmp_path: Path):
    db_path = tmp_path / "p.jsonl"
    md_path = tmp_path / "p.md"
    db = PermanentDB(db_path)
    db.append(PermanentEntry(
        id="e1", captured_at="2026-04-17T10:00", source_group="G1", source_sender="A",
        category="invite_code", type="permanent", title="Bitget invite", content="CODE",
    ))
    db.append(PermanentEntry(
        id="e2", captured_at="2026-04-17T11:00", source_group="G1", source_sender="B",
        category="bank_product", type="product", title="恒生线上开户", content="CNID",
    ))
    regenerate_permanent_md(db_path, md_path)

    text = md_path.read_text(encoding="utf-8")
    assert "# 永久机会库" in text
    assert "邀请码" in text or "invite_code" in text
    assert "Bitget invite" in text
    assert "恒生线上开户" in text
    assert "e1" in text


def test_regenerate_escapes_pipe_in_title(tmp_path: Path):
    db_path = tmp_path / "p.jsonl"
    md_path = tmp_path / "p.md"
    db = PermanentDB(db_path)
    db.append(PermanentEntry(
        id="e1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent",
        title="Plan A | Plan B", content="code|value",
    ))
    regenerate_permanent_md(db_path, md_path)
    text = md_path.read_text(encoding="utf-8")
    # Literal pipes should be escaped (\\|)
    assert "Plan A \\| Plan B" in text
    assert "code\\|value" in text
