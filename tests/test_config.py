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
  model: "legacy-summary-model"
  api_key_env: "LEGACY_API_KEY"
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
    assert cfg.llm.model == "legacy-summary-model"
    assert cfg.models.summary.model == "legacy-summary-model"
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


def test_load_config_reads_multi_model_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups: ["微信 A"]
models:
  summary:
    endpoint: "https://api.deepseek.com"
    model: "deepseek-v4-pro"
    api_key_env: "DEEPSEEK_API_KEY"
  vision:
    enabled: true
    endpoint: "https://vision.example/v1"
    model: "gemini"
    api_key_env: "VISION_API_KEY"
  image:
    enabled: true
    mode: "auto"
    endpoint: "http://127.0.0.1:8317/v1"
    model: "gpt-image-2"
    api_key_env: "IMAGE_API_KEY"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.llm.model == "deepseek-v4-pro"
    assert cfg.models.summary.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.models.vision.enabled is True
    assert cfg.models.vision.model == "gemini"
    assert cfg.models.image.mode == "auto"
