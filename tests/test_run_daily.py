from pathlib import Path
from unittest.mock import patch, MagicMock


def test_run_daily_pipeline_mocks(tmp_path, monkeypatch):
    """Mock wx_exporter, llm, tg, and verify orchestrator ties them together."""
    # Patch DATA_DIR to tmp
    import wx_daily_tg.paths as paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.yaml")

    # Write a minimal config
    (tmp_path / "config.yaml").write_text(
        """
groups: [G1]
llm: {endpoint: "http://x", model: "m", api_key_env: "K", max_tokens: 100}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("K", "fake")
    monkeypatch.setenv("TT", "fake")
    monkeypatch.setenv("TC", "123")

    llm_content = (
        "```markdown concise\nConcise\n```\n\n"
        "```markdown detailed\nDetailed\n```\n\n"
        "```json opportunities\n"
        '{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}\n'
        "```"
    )

    with patch("wx_daily_tg.wx_exporter.subprocess.run") as run, \
         patch("wx_daily_tg.llm_client.LLMClient.chat") as mock_chat, \
         patch("wx_daily_tg.tg_sender.httpx.Client") as tg_client_cls:
        # wx subprocess writes a file at -o path
        def fake_run(cmd, **kw):
            i = cmd.index("-o")
            Path(cmd[i+1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[i+1]).write_text("# group export\nsample\n", encoding="utf-8")
            return MagicMock(returncode=0, stdout="已导出 3 条消息", stderr="")
        run.side_effect = fake_run

        # llm.chat returns (content, usage)
        mock_chat.return_value = (llm_content, {"total_tokens": 10})

        # tg sender
        tg_resp = MagicMock()
        tg_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        tg_resp.raise_for_status = MagicMock()
        tg_client_cls.return_value.__enter__.return_value.post.return_value = tg_resp

        import run_daily
        monkeypatch.setattr(run_daily, "CONFIG_PATH", tmp_path / "config.yaml")
        rc = run_daily.main(date_str="2026-04-17")
        assert rc == 0

    # Verify archive file was written
    summary_path = tmp_path / "archive" / "2026" / "04" / "17" / "summary.md"
    assert summary_path.exists()
    assert "Detailed" in summary_path.read_text(encoding="utf-8")
