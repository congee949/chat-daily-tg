from pathlib import Path
from unittest.mock import patch, MagicMock
from wx_daily_tg.wx_exporter import export_group


def test_export_group_builds_correct_command(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("wx_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="已导出 42 条消息", stderr="")
        result = export_group(
            group_name="贝利VIP",
            since="2026-04-17",
            until="2026-04-18",
            out_path=out_path,
        )
    # confirm subprocess called with correct args
    called_args = run.call_args[0][0]
    assert called_args[0].endswith("wx")
    assert "export" in called_args
    assert "贝利VIP" in called_args
    assert "--since" in called_args
    assert "2026-04-17" in called_args
    assert "--until" in called_args
    assert "2026-04-18" in called_args
    assert "--format" in called_args
    assert "markdown" in called_args
    assert "-o" in called_args
    assert str(out_path) in called_args
    assert result.message_count == 42


def test_export_group_nonzero_exit_raises(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("wx_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="group not found")
        import pytest
        with pytest.raises(RuntimeError, match="group not found"):
            export_group("missing", "2026-04-17", "2026-04-18", out_path)
