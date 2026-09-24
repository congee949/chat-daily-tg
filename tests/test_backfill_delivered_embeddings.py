from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from chat_daily_tg.evidence_index import EmbeddingGeneration


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backfill_delivered_embeddings.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_delivered_embeddings", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_config(path: Path, *, provider: str = "openai") -> None:
    path.write_text(
        """
sources:
  wechat:
    groups: ["test"]
models:
  summary: {endpoint: "http://summary", model: "summary", api_key_env: "SUMMARY_KEY"}
  embedding:
    enabled: true
    provider: %s
    endpoint: "http://127.0.0.1:9999/v1"
    model: "qwen-test"
    api_key_env: ""
    generation_id: "backfill-test-v1"
    model_revision: "weights-test"
    dimension: 2
    normalized: true
telegram: {bot_token_env: "TG_TOKEN", chat_id_env: "TG_CHAT"}
""" % provider,
        encoding="utf-8",
    )


def _write_legacy_db(path: Path, *, rows: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE delivered (
            msg_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, producer TEXT NOT NULL,
            thread_id INTEGER, text TEXT NOT NULL, norm_text TEXT NOT NULL,
            embedding TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta(key,value) VALUES('hwm','777');
    """)
    conn.executemany(
        "INSERT INTO delivered VALUES (?,?,?,?,?,?,NULL)",
        [
            (
                msg_id,
                "2099-01-01T00:00:00+00:00",
                "chatdaily_raw",
                None,
                f"original text {msg_id}",
                f"normalized text {msg_id}",
            )
            for msg_id in range(1, rows + 1)
        ],
    )
    conn.commit()
    conn.close()


class FakeEmbedder:
    generation = EmbeddingGeneration(
        generation_id="backfill-test-v1",
        model_id="qwen-test",
        model_revision="weights-test",
        dimension=2,
        normalized=True,
    )
    batch_size = 1
    supports_deadline = True

    def embed_documents(self, texts, *, deadline=None):
        return [[1.0, 0.0] for _ in texts]


def test_dry_run_is_byte_for_byte_read_only_and_needs_no_embedder(tmp_path, monkeypatch, capsys):
    module = _load_script()
    db = tmp_path / "delivered.db"
    cfg = tmp_path / "config.yaml"
    _write_legacy_db(db)
    _write_config(cfg)
    before = db.read_bytes()
    monkeypatch.setattr(
        module,
        "build_embedder",
        lambda _em: (_ for _ in ()).throw(AssertionError("dry-run must not build client")),
    )

    rc = module.main(["--db", str(db), "--config", str(cfg)])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert payload["status"] == "dry-run"
    assert payload["database_unchanged"] is True
    assert db.read_bytes() == before
    assert not Path(f"{db}-wal").exists()


def test_apply_respects_row_limit_and_preserves_source_columns_and_hwm(
    tmp_path, monkeypatch, capsys
):
    module = _load_script()
    db = tmp_path / "delivered.db"
    cfg = tmp_path / "config.yaml"
    _write_legacy_db(db, rows=2)
    _write_config(cfg)
    monkeypatch.setattr(module, "build_embedder", lambda _em: FakeEmbedder())

    rc = module.main(
        [
            "--db", str(db), "--config", str(cfg), "--apply",
            "--max-rows", "1", "--batch-size", "1", "--max-seconds", "30",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert payload["updated"] == 1
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT msg_id,text,ts,embedding,generation_id,model_id,dimension "
            "FROM delivered ORDER BY msg_id"
        ).fetchall()
        hwm = conn.execute("SELECT value FROM meta WHERE key='hwm'").fetchone()[0]
    finally:
        conn.close()
    assert [row[1] for row in rows] == ["original text 1", "original text 2"]
    assert all(row[2] == "2099-01-01T00:00:00+00:00" for row in rows)
    assert sum(row[3] is not None for row in rows) == 1
    tagged = next(row for row in rows if row[3] is not None)
    assert tagged[4:] == ("backfill-test-v1", "qwen-test", 2)
    assert hwm == "777"


def test_apply_stops_starting_batches_after_wall_clock_limit(tmp_path, monkeypatch, capsys):
    module = _load_script()
    db = tmp_path / "delivered.db"
    cfg = tmp_path / "config.yaml"
    _write_legacy_db(db, rows=2)
    _write_config(cfg)
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = Clock()

    class SlowEmbedder(FakeEmbedder):
        def embed_documents(self, texts, *, deadline=None):
            clock.now = 11.0
            return super().embed_documents(texts, deadline=deadline)

    monkeypatch.setattr(module, "build_embedder", lambda _em: SlowEmbedder())
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    rc = module.main(
        [
            "--db", str(db), "--config", str(cfg), "--apply",
            "--max-rows", "2", "--batch-size", "1", "--max-seconds", "10",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    # The response arrived after the single absolute deadline. No vector
    # transaction is committed and no later batch starts.
    assert payload["updated"] == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM delivered WHERE embedding IS NOT NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_rejects_non_openai_provider_before_schema_mutation(tmp_path):
    module = _load_script()
    db = tmp_path / "delivered.db"
    cfg = tmp_path / "config.yaml"
    _write_legacy_db(db)
    _write_config(cfg, provider="gemini")
    before = db.read_bytes()

    with pytest.raises(ValueError, match="provider=openai"):
        module.main(["--db", str(db), "--config", str(cfg), "--apply"])

    assert db.read_bytes() == before


def test_rejects_zero_row_budget_before_opening_database(tmp_path):
    module = _load_script()
    db = tmp_path / "delivered.db"
    cfg = tmp_path / "config.yaml"
    _write_legacy_db(db)
    _write_config(cfg)
    before = db.read_bytes()

    with pytest.raises(SystemExit) as exc:
        module.main(
            ["--db", str(db), "--config", str(cfg), "--apply", "--max-rows", "0"]
        )

    assert exc.value.code == 2
    assert db.read_bytes() == before


def test_sent_ledger_import_backfill_is_explicit_and_preserves_hwm(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone
    import hashlib
    from chat_daily_tg.sent_content_mirror import write_snapshot
    module = _load_script()
    db, cfg, mirror = (tmp_path / name for name in ("index.db", "config.yaml", "mirror.json"))
    _write_legacy_db(db, rows=0)
    _write_config(cfg)
    content = "An official model launch announcement with full pricing and availability."
    row = dict(schema="sent-content.v1", delivery_state="confirmed", producer="x_monitor",
               chat_id=-1004424841223, message_id=900, thread_id=19, content=content,
               content_hash=hashlib.sha256(content.encode()).hexdigest(),
               sent_at=datetime.now(timezone.utc).isoformat())
    write_snapshot(json.dumps(row).encode(), mirror, source="fixture")
    before = db.read_bytes()
    args = ["--db", str(db), "--config", str(cfg), "--sent-ledger", str(mirror)]
    assert module.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mirror_rows_available"] == 1 and db.read_bytes() == before
    monkeypatch.setattr(module, "build_embedder", lambda _em: FakeEmbedder())
    assert module.main(args + ["--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mirror_rows_imported"] == payload["updated"] == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='hwm'").fetchone()[0] == "777"
        text, embedding = conn.execute("SELECT text,embedding FROM delivered WHERE msg_id=900").fetchone()
        assert text == content and isinstance(embedding, bytes)
    finally:
        conn.close()
