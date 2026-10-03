from pathlib import Path
import pytest
from chat_daily_tg.config import Config, RerankerModel, load_config


def test_energy_usage_defaults_are_opt_in_and_readonly():
    from chat_daily_tg.config import EnergyUsage

    cfg = EnergyUsage()
    assert cfg.enabled is False
    assert cfg.ssh_host == "r4s"
    assert cfg.recorder_uri == "file:/opt/homeassistant/config/home-assistant_v2.db?mode=ro"
    assert cfg.entity_id == "sensor.cuco_v3_1a15_power_cost_today"
    assert cfg.ledger_path == Path("~/chat-daily/state/energy/daily.json")
    assert cfg.timeout_seconds == 20


def test_load_config_reads_energy_usage(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
groups: [G1]
llm: {endpoint: "http://x", model: "m", api_key_env: "K"}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
energy_usage:
  enabled: true
  ssh_host: fixture-host
  recorder_uri: "file:/fixtures/ha.db?mode=ro"
  entity_id: sensor.fixture_energy
  ledger_path: "/fixtures/daily.json"
  timeout_seconds: 8
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.energy_usage.enabled is True
    assert cfg.energy_usage.ssh_host == "fixture-host"
    assert cfg.energy_usage.ledger_path == Path("/fixtures/daily.json")
    assert cfg.energy_usage.timeout_seconds == 8


@pytest.mark.parametrize("uri", [
    "/tmp/ha.db", "file:/tmp/ha.db", "file:/tmp/ha.db?mode=rw",
    "file:/tmp/ha.db?mode=ro&mode=rw",
])
def test_energy_usage_rejects_writable_recorder_uri(uri):
    from chat_daily_tg.config import EnergyUsage

    with pytest.raises(ValueError, match="mode=ro"):
        EnergyUsage(recorder_uri=uri)


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": 121},
    {"ssh_host": ""}, {"ssh_host": "-oProxyCommand=bad"},
    {"entity_id": "sensor.bad entity"},
])
def test_energy_usage_rejects_invalid_source_settings(kwargs):
    from chat_daily_tg.config import EnergyUsage

    with pytest.raises(ValueError):
        EnergyUsage(**kwargs)


def test_xmonitor_ledger_import_is_opt_in_by_default():
    from chat_daily_tg.config import DedupTopic

    assert DedupTopic().xmonitor_ledger_enabled is False


def test_jev_defaults_are_disabled_and_bounded():
    from chat_daily_tg.config import JevModel
    model = JevModel()
    assert model.enabled is False
    assert model.endpoint.endswith('/v1/systemone')
    assert model.model == 'jev-latest'
    assert model.api_key_env == 'TYPESAFE_API_KEY'
    assert model.retry_max_attempts == 2


@pytest.mark.parametrize("retired_value", [0, "retired"])
def test_retired_jev_limits_are_ignored_in_legacy_config(tmp_path, monkeypatch, retired_value):
    import yaml
    from chat_daily_tg import paths
    from chat_daily_tg.jev_judge import build_jev_judge
    from chat_daily_tg.jev_shadow import build_shadow

    retired = dict.fromkeys((
        "jev_judge_max_attempts_per_run", "jev_judge_daily_cap",
        "jev_shadow_max_calls_per_run", "jev_shadow_daily_cap",
    ), retired_value)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "groups": ["Test group"],
        "models": {
            "summary": {"endpoint": "https://example.test/v1", "model": "test", "api_key_env": "TEST_KEY"},
            "jev": {"enabled": True},
        },
        "telegram": {"bot_token_env": "TG_TEST_KEY", "chat_id_env": "TG_TEST_CHAT"},
        "sources": {"telegram": {"dedup": {"topic": {
            **retired, "judge_provider": "jev", "jev_shadow_enabled": True,
        }}}},
    }))
    cfg = load_config(cfg_file)
    topic = cfg.sources.telegram.dedup.topic
    assert not retired.keys() & topic.model_dump().keys()
    assert topic.max_judge_calls_per_run == 5
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(paths, "JEV_DEDUP_JUDGE", tmp_path / "judge.jsonl")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    judge = build_jev_judge(cfg)
    shadow = build_shadow(cfg)
    assert judge is not None and shadow is not None
    judge.close()
    shadow.client.close()
    assert not list(tmp_path.glob("*.budget.json"))


def test_reranker_revision_attestation_is_opt_in_by_default():
    model = RerankerModel(
        endpoint="https://generic-reranker.test/v1",
        model="generic-reranker",
        api_key_env="",
    )

    assert model.model_revision == ""


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
    assert cfg.archive.media_retention_days == 14  # default, not set in this fixture


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
      - id: "-1001234567890"
        name: "示例TG群A"
        limit: 50
        include_patterns: ["(?i)有效信息"]
        exclude_senders: ["Group Help Bot"]
        exclude_patterns: ["(?i)入群验证"]
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
    assert cfg.sources.telegram.chats[0].id == "-1001234567890"
    assert cfg.sources.telegram.chats[0].limit == 50
    assert cfg.sources.telegram.chats[0].include_patterns == ["(?i)有效信息"]
    assert cfg.sources.telegram.chats[0].exclude_senders == ["Group Help Bot"]
    assert cfg.sources.telegram.chats[0].exclude_patterns == ["(?i)入群验证"]
    assert cfg.sources.telegram.sync_before_export is False


def test_load_config_rejects_invalid_daily_telegram_exclude_regex(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  telegram:
    enabled: true
    chats:
      - id: "-1001"
        name: "TG"
        exclude_patterns: ["["]
llm: {endpoint: "http://x", model: "m", api_key_env: "K"}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Telegram message regex"):
        load_config(cfg_file)


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
  embedding:
    enabled: true
    endpoint: "https://generativelanguage.googleapis.com/v1beta"
    model: "gemini-embedding-2"
    api_key_env: "GOOGLE_API_KEY"
    dimension: 768
    top_k: 6
    min_similarity: 0.4
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
    assert cfg.models.embedding.enabled is True
    assert cfg.models.embedding.model == "gemini-embedding-2"
    assert cfg.models.embedding.provider == "gemini"
    assert cfg.models.embedding.batch_size == 100
    assert cfg.models.embedding.top_k == 6
    assert cfg.models.embedding.min_similarity == 0.4


def test_load_config_reads_local_openai_embedding_without_api_key(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups: ["微信 A"]
models:
  summary: {endpoint: "http://summary", model: "gpt-5.6-sol", api_key_env: "K"}
  embedding:
    enabled: true
    provider: openai
    endpoint: "http://127.0.0.1:8790/v1"
    model: "qwen3-vl-embedding-8b-4bit"
    batch_size: 32
    dimension: 4096
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert cfg.models.embedding.provider == "openai"
    assert cfg.models.embedding.api_key_env == ""
    assert cfg.models.embedding.batch_size == 32
    assert cfg.models.embedding.dimension == 4096


def test_load_config_resolves_sol_alias(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups: ["微信 A"]
sol: &sol
  endpoint: "http://127.0.0.1:8317/v1"
  model: "gpt-5.6-sol"
  api_key_env: "CLIPROXY_API_KEY"
models:
  summary: *sol
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert cfg.resolve_model_alias("sol").model == "gpt-5.6-sol"


def test_load_config_reads_channel_exclusions_and_health_briefing(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  telegram:
    enabled: true
    raw_channels:
      - id: "-1001"
        name: "channel"
        username: "channel"
        exclude_patterns: ['(?m)^#morning\\s*$']
llm:
  endpoint: "https://example.test"
  model: "m"
  api_key_env: "K"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
health_briefing:
  enabled: true
  export_dir: "~/HealthExport/AutoSync"
  baseline_days: 21
  min_baseline_samples: 5
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.sources.telegram.raw_channels[0].exclude_patterns == [r"(?m)^#morning\s*$"]
    assert cfg.health_briefing.enabled is True
    assert cfg.health_briefing.baseline_days == 21
    assert cfg.health_briefing.min_baseline_samples == 5


def test_resolve_model_alias_and_growth_judge_models(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups: ["微信 A"]
llm:
  endpoint: "https://api.deepseek.com"
  model: "deepseek-v4-pro"
  api_key_env: "DEEPSEEK_API_KEY"
sonnet:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "claude-sonnet-4-6"
  api_key_env: "CLIPROXY_API_KEY"
gemini:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "gemini-3.6-flash-high"
  api_key_env: "CLIPROXY_API_KEY"
growth:
  enabled: true
  judge_model: "sonnet"
  judge_fallback_model: "gemini"
  source:
    id: "-1001162433032"
    name: "电丸朱氏会社"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.growth.judge_model == "sonnet"
    assert cfg.growth.judge_fallback_model == "gemini"
    assert cfg.resolve_model_alias("sonnet").model == "claude-sonnet-4-6"
    # judge alias must not touch the summary/miner model
    assert cfg.models.summary.model == "deepseek-v4-pro"
    with pytest.raises(KeyError):
        cfg.resolve_model_alias("nope")


def test_resolve_vibekey_model_alias(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  wechat:
    groups: ["test"]
llm:
  endpoint: "https://example.test/v1"
  model: "default-model"
  api_key_env: "DEFAULT_API_KEY"
vibekey:
  endpoint: "https://api.vibekey.cn/v1"
  model: "test-model"
  api_key_env: "VIBEKEY_API_KEY"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)
    assert cfg.resolve_model_alias("vibekey").endpoint == "https://api.vibekey.cn/v1"
    cfg.override_summary_model("vibekey")
    assert cfg.models.summary.model == "test-model"


def test_load_config_dedup_layers_roundtrip_and_defaults(tmp_path: Path):
    # Explicit dedup section + a per-channel opt-out.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
sources:
  telegram:
    enabled: true
    raw_channels:
      - id: "-1001"
        name: "a"
        username: "a"
      - id: "-1002"
        name: "b"
        username: "b"
        dedup: false
    dedup:
      content:
        window_days: 7
      topic:
        enabled: true
        mode: "annotate"
llm:
  endpoint: "https://example.test"
  model: "m"
  api_key_env: "K"
vibekey:
  endpoint: "https://api.vibekey.example/v1"
  model: "gpt-5.6-sol"
  api_key_env: "VIBEKEY_API_KEY"
models:
  summary: {endpoint: "https://example.test", model: "m", api_key_env: "K"}
  embedding:
    enabled: true
    endpoint: "https://gemini.example"
    model: "gemini-embedding-2"
    api_key_env: "GOOGLE_API_KEY"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    d = cfg.sources.telegram.dedup
    assert d.content.window_days == 7
    assert d.content.enabled is True                # default holds in partial section
    assert d.topic.enabled is True
    assert d.topic.mode == "annotate"
    assert d.topic.enforce_enabled is False
    assert d.topic.reranker_enabled is False
    assert d.topic.reranker_top_k == 3
    assert cfg.semantic_features.knowledge_canary_enabled is False
    assert cfg.semantic_features.knowledge_retrieval_enabled is False
    assert cfg.semantic_features.daily_evidence_reranker_enabled is False
    assert d.topic.judge_model == "gpt-5.6-terra"   # unset field keeps its default
    assert cfg.sources.telegram.raw_channels[0].dedup is True   # default opt-in
    assert cfg.sources.telegram.raw_channels[1].dedup is False  # explicit opt-out

    # Enabling the topic layer with a broken coupling (missing judge alias or
    # embedding) must fail at CONFIG LOAD — runtime failure silently degrades
    # to "layer off every run", which is unobservable for weeks.
    import pytest as _pytest
    broken = cfg_file.read_text(encoding="utf-8").replace(
        "vibekey:\n  endpoint: \"https://api.vibekey.example/v1\"\n"
        "  model: \"gpt-5.6-sol\"\n  api_key_env: \"VIBEKEY_API_KEY\"\n", "")
    cfg_file_broken = cfg_file.parent / "broken.yaml"
    cfg_file_broken.write_text(broken, encoding="utf-8")
    with _pytest.raises(ValueError, match="judge_model_alias"):
        load_config(cfg_file_broken)

    # dedup section entirely absent → full defaults.
    cfg_file2 = tmp_path / "config2.yaml"
    cfg_file2.write_text(
        """
sources:
  telegram:
    enabled: true
    raw_channels:
      - id: "-1001"
        name: "a"
        username: "a"
llm:
  endpoint: "https://example.test"
  model: "m"
  api_key_env: "K"
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
""",
        encoding="utf-8",
    )
    cfg2 = load_config(cfg_file2)
    d2 = cfg2.sources.telegram.dedup
    assert d2.content.enabled is True
    assert d2.content.window_days == 14
    assert d2.topic.enabled is False
    assert d2.topic.mode == "report"
    assert d2.topic.enforce_enabled is False
    assert d2.topic.reranker_enabled is False
    assert d2.topic.judge_model == "gpt-5.6-terra"
    assert cfg2.sources.telegram.raw_channels[0].dedup is True


def test_semantic_release_flags_are_explicit_and_independent(tmp_path: Path):
    cfg_file = tmp_path / "flags.yaml"
    cfg_file.write_text(
        """
sources:
  wechat: {groups: [test]}
  telegram:
    dedup:
      topic:
        mode: enforce
        enforce_enabled: false
        reranker_enabled: true
semantic_features:
  knowledge_canary_enabled: true
  knowledge_retrieval_enabled: true
  daily_evidence_reranker_enabled: false
models:
  summary: {endpoint: "http://summary", model: "summary", api_key_env: "K"}
  reranker:
    enabled: true
    endpoint: "http://127.0.0.1:8790/v1"
    model: "qwen-reranker"
    model_revision: "reranker-fingerprint-test"
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert cfg.semantic_features.knowledge_canary_enabled is True
    assert cfg.semantic_features.knowledge_retrieval_enabled is True
    assert cfg.semantic_features.daily_evidence_reranker_enabled is False
    assert cfg.sources.telegram.dedup.topic.enforce_enabled is False
    assert cfg.sources.telegram.dedup.topic.reranker_enabled is True
    # Merely enabling the shared reranker model toggles neither daily rerank
    # nor L2 enforcement.
    assert cfg.models.reranker.enabled is True
    assert cfg.models.reranker.model_revision == "reranker-fingerprint-test"
    assert cfg.models.reranker.timeout == 8.0
