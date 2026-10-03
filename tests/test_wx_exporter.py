import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from chat_daily_tg.media import MediaCandidate
from chat_daily_tg.wx_exporter import (
    _download_wx_images,
    _select_wx_binary,
    clean_wx_markdown,
    export_group,
)


def _write_executable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)


def _make_wx_npm_install(tmp_path: Path, *, dependency_os: str = "darwin") -> tuple[Path, Path]:
    package_root = tmp_path / "node_modules" / "@jackwener" / "wx-cli"
    wrapper = package_root / "bin" / "wx.js"
    _write_executable(wrapper, b"#!/usr/bin/env node\n")
    dependency = "@jackwener/wx-cli-darwin-arm64"
    (package_root / "package.json").write_text(
        json.dumps({
            "name": "@jackwener/wx-cli",
            "bin": {"wx": "bin/wx.js"},
            "optionalDependencies": {dependency: "0.3.0"},
        }),
        encoding="utf-8",
    )
    native_root = package_root / "node_modules" / "@jackwener" / "wx-cli-darwin-arm64"
    native_root.mkdir(parents=True)
    (native_root / "package.json").write_text(
        json.dumps({
            "name": dependency,
            "os": [dependency_os],
            "cpu": ["arm64"],
        }),
        encoding="utf-8",
    )
    native = native_root / "bin" / "wx"
    _write_executable(native, b"\xcf\xfa\xed\xfe" + b"native")
    return wrapper, native


def test_select_wx_binary_prefers_verified_platform_package(tmp_path: Path):
    wrapper, native = _make_wx_npm_install(tmp_path)
    with patch.dict("os.environ", {"WX_CLI_BINARY": str(wrapper)}), patch(
        "chat_daily_tg.wx_exporter.sys.platform", "darwin"
    ), patch(
        "chat_daily_tg.wx_exporter.platform.machine", return_value="arm64"
    ):
        assert _select_wx_binary() == str(native)


def test_select_wx_binary_keeps_explicit_native_binary(tmp_path: Path):
    native = tmp_path / "wx-native"
    _write_executable(native, b"\x7fELF" + b"native")
    with patch.dict("os.environ", {"WX_CLI_BINARY": str(native)}):
        assert _select_wx_binary() == str(native)


def test_select_wx_binary_falls_back_for_unowned_wrapper(tmp_path: Path):
    wrapper, _ = _make_wx_npm_install(tmp_path)
    package_json = wrapper.parent.parent / "package.json"
    package_json.write_text(
        json.dumps({"bin": {"wx": "bin/some-other-script.js"}}), encoding="utf-8"
    )
    with patch.dict("os.environ", {"WX_CLI_BINARY": str(wrapper)}), patch(
        "chat_daily_tg.wx_exporter.sys.platform", "darwin"
    ), patch(
        "chat_daily_tg.wx_exporter.platform.machine", return_value="arm64"
    ):
        assert _select_wx_binary() == str(wrapper)


def test_select_wx_binary_falls_back_for_platform_mismatch(tmp_path: Path):
    wrapper, _ = _make_wx_npm_install(tmp_path, dependency_os="linux")
    with patch.dict("os.environ", {"WX_CLI_BINARY": str(wrapper)}), patch(
        "chat_daily_tg.wx_exporter.sys.platform", "darwin"
    ), patch(
        "chat_daily_tg.wx_exporter.platform.machine", return_value="arm64"
    ):
        assert _select_wx_binary() == str(wrapper)


def test_export_group_captures_stdout_and_writes_cleaned(tmp_path: Path):
    out_path = tmp_path / "out.md"
    stdout = (
        "# 群聊\n\n> 导出 42 条消息\n\n"
        "### 2026-04-17 10:00\n\n**Alice**: 真消息[Laugh]\n\n"
        "### 2026-04-17 10:01\n\n[系统] 邀请\n"
    )
    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        result = export_group(
            group_name="示例微信群A",
            since="2026-04-17",
            until="2026-04-18",
            out_path=out_path,
        )
    called_args = run.call_args[0][0]
    assert called_args[0].endswith("wx")
    assert "export" in called_args and "示例微信群A" in called_args
    assert "--since" in called_args and "2026-04-17" in called_args
    assert "--format" in called_args and "markdown" in called_args
    assert "-o" not in called_args  # stdout capture, no file arg
    assert result.message_count == 42
    assert "[Laugh]" not in result.content
    assert "[系统]" not in result.content
    assert "**Alice**: 真消息" in result.content
    assert out_path.read_text(encoding="utf-8") == result.content


def test_export_group_retries_timeout_then_succeeds(tmp_path: Path):
    out_path = tmp_path / "out.md"
    stdout = "# 群聊\n\n> 导出 3 条消息\n\n### 2026-04-17 10:00\n\n**Alice**: 真消息\n"
    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[
            subprocess.TimeoutExpired(cmd="wx", timeout=30),
            MagicMock(returncode=0, stdout=stdout, stderr=""),
        ],
    ) as run:
        result = export_group("示例微信群A", "2026-04-17", "2026-04-17", out_path)
    assert run.call_count == 2
    assert result.message_count == 3


def test_export_group_timeout_exhausted_raises(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="wx", timeout=30),
    ):
        import pytest
        with pytest.raises(RuntimeError, match="timed out"):
            export_group("示例微信群A", "2026-04-17", "2026-04-17", out_path)


def test_export_group_hard_error_exhausted_preserves_output(tmp_path: Path):
    """Repeated daemon cache failures must not replace the prior archive."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "既有的非空微信归档\n"
    out_path.write_text(original, encoding="utf-8")
    hard_error = MagicMock(
        returncode=1,
        stdout="",
        stderr="错误: 全量解密后不是合法 SQLite\n",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", return_value=hard_error
    ) as run, patch(
        "chat_daily_tg.wx_exporter.extract_wx_media_candidates"
    ) as extract_media, patch(
        "chat_daily_tg.wx_exporter._download_wx_images"
    ) as download_images:
        with pytest.raises(RuntimeError, match="全量解密后不是合法 SQLite"):
            export_group("示例微信群A", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original
    download_images.assert_not_called()


def test_export_group_recovers_from_hard_error_then_writes_success(tmp_path: Path):
    """A transient hard cache failure may recover on a later export attempt."""
    out_path = tmp_path / "out.md"
    out_path.write_text("旧归档\n", encoding="utf-8")
    successful_export = (
        "# 示例微信群A（群聊）\n\n"
        "> 导出 2 条消息\n\n"
        "### 2026-04-17 10:00\n\n**Alice**: 恢复后的消息\n"
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[
            MagicMock(
                returncode=1,
                stdout="",
                stderr="错误: 全量解密后不是合法 SQLite\n",
            ),
            MagicMock(returncode=0, stdout=successful_export, stderr=""),
        ],
    ) as run:
        result = export_group(
            "示例微信群A", "2026-04-17", "2026-04-17", out_path
        )

    assert run.call_count == 2
    assert result.message_count == 2
    assert "**Alice**: 恢复后的消息" in result.content
    assert out_path.read_text(encoding="utf-8") == result.content
    assert "旧归档" not in result.content


def test_export_group_rejects_zero_with_stale_warning_and_preserves_output(tmp_path: Path):
    """A warning-backed zero must not overwrite output during daemon warmup.

    The temporary database deliberately keeps the new row in WAL while a reader
    pins the older snapshot.  The fake ``wx export`` process opens a fresh SQLite
    view and checks that row exists, then returns the cold-daemon shape: zero
    exported messages plus a warning that the result may be incomplete.
    """
    db_path = tmp_path / "message_0.db"
    writer = sqlite3.connect(db_path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "CREATE TABLE messages (chat TEXT, timestamp INTEGER, content TEXT)"
    )
    writer.commit()

    pinned_reader = sqlite3.connect(db_path)
    pinned_reader.execute("BEGIN")
    assert pinned_reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    writer.execute(
        "INSERT INTO messages VALUES (?, ?, ?)",
        ("示例微信群A", 1_776_355_200, "WAL 中的新消息"),
    )
    writer.commit()

    wal_path = Path(f"{db_path}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0
    with sqlite3.connect(db_path) as direct_view:
        assert direct_view.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

    out_path = tmp_path / "out.md"
    original = "既有的非空微信归档\n"
    out_path.write_text(original, encoding="utf-8")
    zero_export = (
        "# 示例微信群A（群聊）\n\n"
        "> 导出 0 条消息\n\n"
        "> [!WARNING]\n"
        "> 磁盘上发现 daemon 不认识的分片 message/message_1.db，"
        "结果可能不完整；运行 `wx init --force` 重新提取密钥。\n"
    )
    warning_stderr = (
        "[wx] 警告：磁盘上发现 daemon 不认识的分片 message/message_1.db，"
        "结果可能不完整；运行 `wx init --force` 重新提取密钥。\n"
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "export" in cmd:
            # This is the independent fact source: the latest message is in
            # the WAL sidecar, not in the pinned reader's old snapshot.
            with sqlite3.connect(db_path) as probe_view:
                actual_count = probe_view.execute(
                    "SELECT COUNT(*) FROM messages WHERE chat = ?",
                    ("示例微信群A",),
                ).fetchone()[0]
            assert actual_count == 1
            return MagicMock(returncode=0, stdout=zero_export, stderr=warning_stderr)
        # An implementation may issue an explicit daemon refresh between
        # attempts; keep that transport detail out of this wrapper contract.
        return MagicMock(returncode=0, stdout="", stderr="")

    caught: RuntimeError | None = None
    try:
        with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
            "chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run
        ):
            export_group(
                "示例微信群A", "2026-04-17", "2026-04-17", out_path
            )
    except RuntimeError as exc:
        caught = exc
    finally:
        pinned_reader.close()
        writer.close()

    assert out_path.read_text(encoding="utf-8") == original
    assert caught is not None
    assert (
        "stale" in str(caught).lower()
        or "zero-count mismatch" in str(caught).lower()
        or "不完整" in str(caught)
    )
    export_calls = [cmd for cmd in calls if "export" in cmd]
    assert len(export_calls) >= 4
    assert all("--with-meta" in cmd for cmd in export_calls)


def test_export_group_remembers_earlier_incomplete_zero_signal(tmp_path: Path):
    """A later silent zero cannot erase an earlier incomplete diagnostic."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    warning_zero = MagicMock(
        returncode=0,
        stdout="# group\n\n> 导出 0 条消息\n> [!WARNING]\n> 结果可能不完整\n",
        stderr="",
    )
    silent_zero = MagicMock(
        returncode=0,
        stdout="# group\n\n> 导出 0 条消息\n",
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[warning_zero, silent_zero, silent_zero, silent_zero],
    ):
        with pytest.raises(RuntimeError, match="zero-count mismatch"):
            export_group("group", "2026-04-17", "2026-04-17", out_path)

    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_rejects_positive_count_with_incomplete_warning(tmp_path: Path):
    """Partial shard results cannot replace an archive merely because count>0."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    partial_positive = MagicMock(
        returncode=0,
        stdout="# group\n\n> 导出 2 条消息\n> [!WARNING]\n> 结果可能不完整\n",
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", return_value=partial_positive
    ) as run:
        with pytest.raises(RuntimeError, match="incomplete result"):
            export_group("group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_earlier_warning_then_clean_positive_recovers(tmp_path: Path):
    """A later complete positive response is allowed to recover a tainted retry."""
    out_path = tmp_path / "out.md"
    out_path.write_text("旧归档\n", encoding="utf-8")
    warning_zero = MagicMock(
        returncode=0,
        stdout="# group\n\n> 导出 0 条消息\n> [!WARNING]\n> 结果可能不完整\n",
        stderr="",
    )
    clean_positive = MagicMock(
        returncode=0,
        stdout="# group\n\n> 导出 1 条消息\n\n### 2026-04-17 10:00\n\n**Alice**: recovered\n",
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[warning_zero, clean_positive],
    ) as run:
        result = export_group("group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 2
    assert result.message_count == 1
    assert "recovered" in out_path.read_text(encoding="utf-8")


def test_export_group_accepts_genuine_zero_without_warning(tmp_path: Path):
    """A quiet window remains a valid empty result when no stale signal exists."""
    out_path = tmp_path / "out.md"
    empty_export = "# 示例微信群A（群聊）\n\n> 导出 0 条消息\n"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert "export" in cmd
        return MagicMock(returncode=0, stdout=empty_export, stderr="")

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run
    ):
        result = export_group("示例微信群A", "2026-04-17", "2026-04-17", out_path)

    assert result.message_count == 0
    assert result.content == empty_export
    assert out_path.read_text(encoding="utf-8") == empty_export
    export_calls = [cmd for cmd in calls if "export" in cmd]
    assert export_calls
    assert all("--with-meta" in cmd for cmd in export_calls)


def test_export_group_zero_then_timeouts_preserves_output(tmp_path: Path):
    """A clean zero followed by timeouts is not enough to erase the archive."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    clean_zero = MagicMock(returncode=0, stdout="# group\n\n> 导出 0 条消息\n", stderr="")

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[clean_zero, subprocess.TimeoutExpired(cmd="wx", timeout=30),
                      subprocess.TimeoutExpired(cmd="wx", timeout=30),
                      subprocess.TimeoutExpired(cmd="wx", timeout=30)],
    ) as run:
        with pytest.raises(RuntimeError, match="timed out"):
            export_group("group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_hard_error_then_clean_zeros_preserves_output(tmp_path: Path):
    """A hard error taints later zeros until a clean positive result arrives."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    hard_error = MagicMock(returncode=1, stdout="", stderr="daemon warming\n")
    clean_zero = MagicMock(returncode=0, stdout="# group\n\n> 导出 0 条消息\n", stderr="")

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        side_effect=[hard_error, clean_zero, clean_zero, clean_zero],
    ) as run:
        with pytest.raises(RuntimeError, match="incomplete result"):
            export_group("group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_rejects_success_without_count_and_preserves_output(tmp_path: Path):
    """A truncated rc=0 response must not be reinterpreted as a genuine zero."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    malformed = MagicMock(
        returncode=0,
        stdout="# example group\n\npartial response without summary\n",
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", return_value=malformed
    ) as run, patch(
        "chat_daily_tg.wx_exporter.extract_wx_media_candidates"
    ) as extract_media:
        with pytest.raises(RuntimeError, match="missing message-count summary"):
            export_group("example group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_does_not_treat_body_text_as_count_summary(tmp_path: Path):
    """A user message containing the summary phrase is not exporter metadata."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    body_only = MagicMock(
        returncode=0,
        stdout="# example group\n\nAlice: 导出 7 条消息\n",
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", return_value=body_only
    ) as run:
        with pytest.raises(RuntimeError, match="missing message-count summary"):
            export_group("example group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_does_not_treat_blockquoted_body_continuation_as_summary(tmp_path: Path):
    """A multiline message continuation can look exactly like the count line."""
    import pytest

    out_path = tmp_path / "out.md"
    original = "existing non-empty archive\n"
    out_path.write_text(original, encoding="utf-8")
    body_continuation_only = MagicMock(
        returncode=0,
        stdout=(
            "# example group\n\n"
            "### 2026-04-17 10:00\n\n"
            "**Alice**: first line\n"
            "> 导出 7 条消息\n"
        ),
        stderr="",
    )

    with patch("chat_daily_tg.wx_exporter.time.sleep"), patch(
        "chat_daily_tg.wx_exporter.subprocess.run", return_value=body_continuation_only
    ) as run:
        with pytest.raises(RuntimeError, match="missing message-count summary"):
            export_group("example group", "2026-04-17", "2026-04-17", out_path)

    assert run.call_count == 4
    assert out_path.read_text(encoding="utf-8") == original


def test_export_group_nonzero_exit_raises(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="group not found")
        import pytest
        with pytest.raises(RuntimeError, match="group not found"):
            export_group("missing", "2026-04-17", "2026-04-18", out_path)


def test_clean_drops_patpat_block():
    raw = (
        "### 2026-04-17 03:02\n\n"
        '[链接] "样例用户B" 拍了拍 "样例用户A"\n\n'
        "### 2026-04-17 04:36\n\n"
        "**样例用户C**: @样例用户B 加你了\n"
    )
    out = clean_wx_markdown(raw)
    assert "拍了拍" not in out
    assert "@样例用户B 加你了" in out


def test_clean_drops_system_block():
    raw = (
        "### 2026-04-17 06:13\n\n"
        '[系统] "样例助手"邀请"新成员"加入了群聊\n\n'
        "### 2026-04-17 09:19\n\n"
        "**Alice**: hi\n"
    )
    out = clean_wx_markdown(raw)
    assert "[系统]" not in out
    assert "邀请" not in out
    assert "**Alice**: hi" in out


def test_clean_strips_inline_emoji_and_image_local_id():
    raw = "**肖🐙**: [图片] local_id=2932\n\n**A**: 恒生可以线上开了[哇]\n"
    out = clean_wx_markdown(raw)
    assert "[图片]" not in out
    assert "local_id" not in out
    assert "[哇]" not in out
    assert "恒生可以线上开了" in out


def test_clean_drops_block_that_becomes_empty_after_stripping_image():
    raw = (
        "### 2026-04-18 09:28\n\n"
        "**肖🐙**: [图片] local_id=2932\n\n"
        "### 2026-04-18 09:28\n\n"
        "**肖🐙**: 卓越plus\n"
    )
    out = clean_wx_markdown(raw)
    assert "**肖🐙**: 卓越plus" in out
    assert out.count("### 2026-04-18 09:28") == 1


def test_clean_handles_english_sticker_names():
    raw = "**A**: 710了[Emm]\n**B**: [OK][Facepalm]hi\n**C**: [Laugh][Laugh]\n"
    out = clean_wx_markdown(raw)
    for tok in ("[Emm]", "[OK]", "[Facepalm]", "[Laugh]"):
        assert tok not in out
    assert "710了" in out and "hi" in out


def test_clean_handles_digit_sticker_and_attachment_localid():
    raw = "**A**: [666]\n**B**: [视频] local_id=99\n**C**: [文件] local_id=100 report.pdf\n"
    out = clean_wx_markdown(raw)
    assert "[666]" not in out
    assert "local_id" not in out
    assert "report.pdf" in out  # file-attachment context preserved


def test_clean_drops_redpacket_and_transfer_blocks():
    raw = (
        "### 2026-04-17 10:00\n\n**A**: [红包]\n\n"
        "### 2026-04-17 10:01\n\n**B**: [转账]\n\n"
        "### 2026-04-17 10:02\n\n**C**: 真消息\n"
    )
    out = clean_wx_markdown(raw)
    assert "[红包]" not in out and "[转账]" not in out
    assert "**A**:" not in out and "**B**:" not in out
    assert "**C**: 真消息" in out


def test_clean_preserves_user_bracketed_phrases_over_10_chars():
    raw = "**A**: 引用里 [这是一个长度超过十个字的用户括号短语] 保留\n"
    out = clean_wx_markdown(raw)
    assert "[这是一个长度超过十个字的用户括号短语]" in out


def test_clean_against_real_fixture():
    """Golden properties on a representative real wx export slice."""
    from pathlib import Path
    raw = (Path(__file__).parent / "fixtures" / "wx_export_raw_sample.md").read_text(encoding="utf-8")
    out = clean_wx_markdown(raw)

    for noise in ("拍了拍", "[系统]", "[煙花]", "[Emm]", "[Laugh]",
                  "[OK]", "[Facepalm]", "[666]", "[红包]", "[引用]",
                  "[图片]", "local_id"):
        assert noise not in out, f"leaked {noise!r} after cleanup"

    for signal in ("**样例用户F**: 你可以问问群主",
                   "**样例用户C**: @样例用户B 加你了",
                   "示例商店",
                   "https://example-pricing.test/",
                   "710了",
                   "这个大妈行",
                   "✈️乘务员是不是能看得见每个乘客的会员等级？"):
        assert signal in out, f"lost signal {signal!r} after cleanup"

    assert "\n\n\n" not in out


def test_export_group_downloads_high_score_wx_images(tmp_path: Path):
    out_path = tmp_path / "out.md"
    raw = (
        "# 群聊\n\n> 导出 2 条消息\n\n"
        "### 2026-04-17 10:00\n\n**A**: 这个活动价格怎么样\n\n"
        "### 2026-04-17 10:01\n\n**A**: [图片] local_id=123\n\n"
    )
    attachments_json = json.dumps({"attachments": [{"local_id": 123, "attachment_id": "abc123"}]})

    def fake_run(cmd, **kwargs):
        if cmd[1] == "export":
            return MagicMock(returncode=0, stdout=raw, stderr="")
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=attachments_json, stderr="")
        if cmd[1] == "extract":
            assert cmd[2] == "abc123"
            out_file = Path(cmd[cmd.index("-o") + 1])
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b"fake-jpeg")
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected wx subcommand: {cmd}")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = export_group("示例微信群A", "2026-04-17", "2026-04-18", out_path)

    assert len(result.media_candidates) == 1
    cand = result.media_candidates[0]
    assert cand.local_path is not None
    assert Path(cand.local_path).read_bytes() == b"fake-jpeg"


def test_export_group_skips_low_score_wx_images_without_any_wx_calls(tmp_path: Path):
    out_path = tmp_path / "out.md"
    raw = (
        "# 群聊\n\n> 导出 1 条消息\n\n"
        "### 2026-04-17 10:00\n\n**A**: [图片] local_id=999\n\n"
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        return MagicMock(returncode=0, stdout=raw, stderr="")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = export_group("示例微信群A", "2026-04-17", "2026-04-18", out_path)

    assert calls == ["export"]  # no attachments/extract call for a below-threshold candidate
    assert result.media_candidates[0].local_path is None


def test_download_wx_images_uses_explicit_min_score(tmp_path: Path):
    """The caller-supplied threshold, including its boundary, controls extraction."""
    candidates = [_cand(1, 0.74), _cand(2, 0.79), _cand(3, 0.80)]
    extract_ids: set[int] = set()

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(
                returncode=0,
                stdout=_attachments_json([1, 2, 3]),
                stderr="",
            )
        assert cmd[1] == "extract"
        extract_ids.add(int(Path(cmd[cmd.index("-o") + 1]).stem))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fake-jpeg-bytes")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = _download_wx_images(
            candidates,
            group_name="G",
            since="2026-08-15",
            until="2026-08-16",
            media_dir=tmp_path / "m",
            min_score=0.80,
        )

    assert extract_ids == {3}
    assert result[0].local_path is None
    assert result[1].local_path is None
    assert result[2].local_path is not None


def test_export_group_passes_explicit_download_threshold(tmp_path: Path):
    out_path = tmp_path / "out.md"
    raw = (
        "# 群聊\n\n> 导出 1 条消息\n\n"
        "### 2026-04-17 10:00\n\n**A**: [图片] local_id=7\n"
    )
    candidates = [_cand(7)]
    with patch(
        "chat_daily_tg.wx_exporter.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=raw, stderr=""),
    ), patch(
        "chat_daily_tg.wx_exporter.extract_wx_media_candidates",
        return_value=candidates,
    ), patch(
        "chat_daily_tg.wx_exporter._download_wx_images",
        return_value=candidates,
    ) as download:
        export_group(
            "示例微信群A",
            "2026-04-17",
            "2026-04-18",
            out_path,
            min_download_score=0.73,
        )

    download.assert_called_once()
    assert download.call_args.kwargs["min_score"] == 0.73


def test_export_group_wx_extract_failure_is_skipped_not_raised(tmp_path: Path):
    out_path = tmp_path / "out.md"
    raw = (
        "# 群聊\n\n> 导出 2 条消息\n\n"
        "### 2026-04-17 10:00\n\n**A**: 这个活动价格怎么样\n\n"
        "### 2026-04-17 10:01\n\n**A**: [图片] local_id=123\n\n"
    )
    attachments_json = json.dumps({"attachments": [{"local_id": 123, "attachment_id": "abc123"}]})

    def fake_run(cmd, **kwargs):
        if cmd[1] == "export":
            return MagicMock(returncode=0, stdout=raw, stderr="")
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=attachments_json, stderr="")
        if cmd[1] == "extract":
            return MagicMock(returncode=1, stdout="", stderr="decrypt failed")
        raise AssertionError(f"unexpected wx subcommand: {cmd}")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = export_group("示例微信群A", "2026-04-17", "2026-04-18", out_path)

    assert result.media_candidates[0].local_path is None


def test_export_group_attachment_lookup_timeout_still_writes_text(tmp_path: Path):
    """Optional attachment lookup failures must not block a valid text export."""
    out_path = tmp_path / "out.md"
    raw = (
        "# 群聊\n\n> 导出 1 条消息\n\n"
        "### 2026-04-17 09:59\n\n**A**: 活动价格\n\n"
        "### 2026-04-17 10:00\n\n**A**: [图片] local_id=123\n"
    )
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        if cmd[1] == "export":
            return MagicMock(returncode=0, stdout=raw, stderr="")
        if cmd[1] == "attachments":
            raise subprocess.TimeoutExpired(cmd="wx attachments", timeout=30)
        raise AssertionError(f"unexpected wx subcommand: {cmd}")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = export_group("示例微信群A", "2026-04-17", "2026-04-18", out_path)

    assert calls == ["export", "attachments"]
    assert result.message_count == 1
    assert result.media_candidates is not None
    assert result.media_candidates[0].local_path is None
    assert out_path.read_text(encoding="utf-8") == result.content
    # The media-only block is intentionally removed by ``clean_wx_markdown``;
    # the neighboring text block proves the正文 was still delivered.
    assert "### 2026-04-17 09:59" in result.content
    assert "活动价格" in result.content


def _cand(local_id: int, score: float = 0.9) -> MediaCandidate:
    return MediaCandidate(
        platform="微信", group_name="G", timestamp="2026-08-16 10:00", sender_name="A",
        media_type="图片", local_path=None, context="活动 价格", reason="t",
        score=score, raw_ref=f"local_id={local_id}",
    )


def _attachments_json(local_ids: list[int]) -> str:
    return json.dumps(
        {"attachments": [{"local_id": i, "attachment_id": f"att-{i}"} for i in local_ids]}
    )


def test_download_wx_images_runs_parallel_and_keeps_order(tmp_path: Path):
    candidates = [_cand(i) for i in (1, 2, 3, 4)]
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=_attachments_json([1, 2, 3, 4]), stderr="")
        assert cmd[1] == "extract"
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fake-jpeg-bytes")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = _download_wx_images(
            candidates, group_name="G", since="2026-08-15", until="2026-08-16",
            media_dir=tmp_path / "m",
        )

    assert [Path(c.local_path).name for c in result] == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert state["peak"] > 1


def test_download_wx_images_isolates_per_item_failures(tmp_path: Path):
    candidates = [_cand(1), _cand(2), _cand(3)]

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=_attachments_json([1, 2, 3]), stderr="")
        out = Path(cmd[cmd.index("-o") + 1])
        if out.name == "2.jpg":
            return MagicMock(returncode=1, stdout="", stderr="decrypt failed")
        if out.name == "3.jpg":
            raise subprocess.TimeoutExpired(cmd="wx", timeout=30)
        out.write_bytes(b"fake-jpeg-bytes")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run):
        result = _download_wx_images(
            candidates, group_name="G", since="2026-08-15", until="2026-08-16",
            media_dir=tmp_path / "m",
        )

    assert result[0].local_path is not None and result[0].local_path.endswith("1.jpg")
    assert result[1].local_path is None
    assert result[2].local_path is None


def test_download_wx_images_decodes_wxgf_extract_output(tmp_path: Path):
    candidates = [_cand(7)]

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=_attachments_json([7]), stderr="")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"wxgf" + b"\x00" * 32)
        return MagicMock(returncode=0, stdout="", stderr="")

    def fake_decode(src: Path, dst: Path) -> bool:
        assert src == dst
        dst.write_bytes(b"\xff\xd8decoded-jpeg")
        return True

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run), \
         patch("chat_daily_tg.wx_exporter.decode_wxgf", side_effect=fake_decode) as decode:
        result = _download_wx_images(
            candidates, group_name="G", since="2026-08-15", until="2026-08-16",
            media_dir=tmp_path / "m",
        )

    assert decode.call_count == 1
    assert result[0].local_path is not None
    assert Path(result[0].local_path).read_bytes() == b"\xff\xd8decoded-jpeg"


def test_download_wx_images_keeps_original_when_wxgf_decode_fails(tmp_path: Path):
    candidates = [_cand(7)]
    wxgf_bytes = b"wxgf" + b"\x00" * 32

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=_attachments_json([7]), stderr="")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(wxgf_bytes)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run), \
         patch("chat_daily_tg.wx_exporter.decode_wxgf", return_value=False) as decode:
        result = _download_wx_images(
            candidates, group_name="G", since="2026-08-15", until="2026-08-16",
            media_dir=tmp_path / "m",
        )

    assert decode.call_count == 1
    assert result[0].local_path is not None
    assert Path(result[0].local_path).read_bytes() == wxgf_bytes


def test_download_wx_images_retries_decode_for_preexisting_wxgf_file(tmp_path: Path):
    media_dir = tmp_path / "m"
    media_dir.mkdir(parents=True)
    (media_dir / "9.jpg").write_bytes(b"wxgf" + b"\x00" * 32)
    candidates = [_cand(9)]
    extract_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[1] == "attachments":
            return MagicMock(returncode=0, stdout=_attachments_json([9]), stderr="")
        extract_calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    def fake_decode(src: Path, dst: Path) -> bool:
        dst.write_bytes(b"\xff\xd8decoded-jpeg")
        return True

    with patch("chat_daily_tg.wx_exporter.subprocess.run", side_effect=fake_run), \
         patch("chat_daily_tg.wx_exporter.decode_wxgf", side_effect=fake_decode) as decode:
        result = _download_wx_images(
            candidates, group_name="G", since="2026-08-15", until="2026-08-16",
            media_dir=media_dir,
        )

    assert extract_calls == []
    assert decode.call_count == 1
    assert Path(result[0].local_path).read_bytes() == b"\xff\xd8decoded-jpeg"
