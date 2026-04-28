from pathlib import Path
import pytest
from chat_daily_tg.config import Config, load_config


def test_load_config_reads_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
groups:
  - "Group A"
  - "Group B"
schedule:
  time: "08:00"
  coverage: "yesterday"
  timezone: "Asia/Shanghai"
hot_leads:
  retention_days: 14
llm:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "claude-sonnet-4-6"
  api_key_env: "CLIPROXY_API_KEY"
  max_tokens: 8000
  extra_body:
    reasoning_effort: "max"
    thinking:
      type: "enabled"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
retry:
  max_attempts: 3
  backoff_seconds: [5, 15, 60]
sanitize:
  enabled: false
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.groups == ["Group A", "Group B"]
    assert cfg.sources.wechat.groups == ["Group A", "Group B"]
    assert cfg.llm.model == "claude-sonnet-4-6"
    assert cfg.llm.endpoint == "http://127.0.0.1:8317/v1"
    assert cfg.llm.extra_body["reasoning_effort"] == "max"
    assert cfg.llm.extra_body["thinking"]["type"] == "enabled"
    assert cfg.hot_leads.retention_days == 14
    assert cfg.schedule.timezone == "Asia/Shanghai"
    assert cfg.sanitize.enabled is False


def test_load_config_missing_groups_raises(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_load_config_reads_multi_source_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups:
      - "微信 A"
  telegram:
    enabled: true
    db_path: "~/Library/Application Support/tg-cli/messages.db"
    sync_before_export: false
    chats:
      - id: "-1003707563960"
        name: "CuiMao爱学习"
        limit: 50
llm:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "m"
  api_key_env: "K"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.groups == ["微信 A"]
    assert cfg.sources.telegram.enabled is True
    assert cfg.sources.telegram.chats[0].id == "-1003707563960"
    assert cfg.sources.telegram.chats[0].limit == 50
    assert cfg.sources.telegram.sync_before_export is False
