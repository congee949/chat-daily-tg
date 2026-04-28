from pathlib import Path
from unittest.mock import patch, MagicMock
from chat_daily_tg.wx_exporter import export_group, clean_wx_markdown


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

    for signal in ("**Lay-**: 你可以问问群主",
                   "**样例用户C**: @样例用户B 加你了",
                   "示例商店",
                   "https://example-pricing.test/",
                   "710了",
                   "这个大妈行",
                   "✈️乘务员是不是能看得见每个乘客的会员等级？"):
        assert signal in out, f"lost signal {signal!r} after cleanup"

    assert "\n\n\n" not in out
