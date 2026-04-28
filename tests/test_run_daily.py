from pathlib import Path
from unittest.mock import patch, MagicMock


def test_run_daily_pipeline_mocks(tmp_path, monkeypatch):
    """Mock chat exporters, llm, tg, and verify orchestrator ties them together."""
    # Patch DATA_DIR to tmp
    import chat_daily_tg.paths as paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(paths, "PERMANENT_JSONL", tmp_path / "permanent.jsonl")
    monkeypatch.setattr(paths, "PERMANENT_MD", tmp_path / "permanent.md")
    monkeypatch.setattr(paths, "HOT_LEADS_DIR", tmp_path / "hot-leads")
    monkeypatch.setattr(paths, "HOT_LEADS_LATEST", tmp_path / "hot-leads" / "latest.md")

    # Write a minimal config
    (tmp_path / "config.yaml").write_text(
        """
sources:
  wechat:
    groups: [G1]
  telegram:
    enabled: true
    db_path: "/tmp/tg.db"
    sync_before_export: false
    chats:
      - id: "-1001"
        name: "TG1"
        limit: 50
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

    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run, \
         patch("run_daily.export_chat") as mock_export_chat, \
         patch("chat_daily_tg.llm_client.LLMClient.chat") as mock_chat, \
         patch("chat_daily_tg.tg_sender.httpx.Client") as tg_client_cls:
        # wx writes markdown to stdout (no -o flag)
        fake_stdout = "# group export\n\n> 导出 3 条消息\n\n### 2026-04-17 10:00\n\n**A**: hi\n"
        run.return_value = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
        mock_export_chat.return_value = MagicMock(
            group_name="TG1",
            message_count=2,
            skipped_count=1,
            content="# Telegram: TG1\n\n[Telegram / TG1 / 10:00 / A] hello\n",
        )

        # llm.chat returns (content, usage)
        mock_chat.return_value = (llm_content, {"total_tokens": 10})

        # tg sender
        tg_resp = MagicMock()
        tg_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        tg_resp.raise_for_status = MagicMock()
        tg_client_instance = tg_client_cls.return_value.__enter__.return_value
        tg_client_instance.post.return_value = tg_resp

        import run_daily
        monkeypatch.setattr(run_daily, "CONFIG_PATH", tmp_path / "config.yaml")
        rc = run_daily.main(date_str="2026-04-17")
        assert rc == 0
        prompt = mock_chat.call_args.args[0]
        assert "### === 群: 微信 / G1 ===" in prompt
        assert "### === 群: Telegram / TG1 ===" in prompt
        assert len(tg_client_instance.post.call_args_list) == 1
        sent_data = tg_client_instance.post.call_args_list[0].kwargs.get("data", {})
        assert sent_data["parse_mode"] == "HTML"

    # Verify archive file was written
    summary_path = tmp_path / "archive" / "2026" / "04" / "17" / "summary.md"
    assert summary_path.exists()
    assert "Detailed" in summary_path.read_text(encoding="utf-8")


def test_run_daily_main_catches_exceptions_and_notifies(tmp_path, monkeypatch):
    """If the pipeline raises, main() should log, notify, and return 1."""
    import chat_daily_tg.paths as paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.yaml")

    # Minimal config that will cause _run to fail (no such config file)
    # Actually — we'll make _run raise by patching it directly
    import run_daily
    monkeypatch.setattr(run_daily, "CONFIG_PATH", tmp_path / "config.yaml")

    # Write a valid config (so load_config succeeds) but make _run raise
    (tmp_path / "config.yaml").write_text(
        """
groups: [G1]
llm: {endpoint: "http://x", model: "m", api_key_env: "K", max_tokens: 100, timeout: 30.0}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )

    from unittest.mock import patch, MagicMock

    # Patch _run to raise an exception
    with patch("run_daily._run", side_effect=RuntimeError("simulated pipeline failure")), \
         patch("run_daily.notify_failure") as notify:
        rc = run_daily.main(date_str="2026-04-17")

    assert rc == 1
    # Notifier should have been called with a failure title
    assert notify.call_count == 1
    call_kwargs = notify.call_args.kwargs if notify.call_args.kwargs else {}
    # Accept either kwarg or positional
    args = notify.call_args.args
    title = call_kwargs.get("title") or (args[0] if args else "")
    message = call_kwargs.get("message") or (args[1] if len(args) > 1 else "")
    assert "chat-daily-tg 失败" in title
    assert "RuntimeError" in message
    assert "simulated pipeline failure" in message
