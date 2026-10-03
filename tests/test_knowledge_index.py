from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from chat_daily_tg import knowledge_index
from chat_daily_tg.knowledge_index import (
    AssetRecord,
    GenerationBuilder,
    GenerationConfig,
    GenerationReader,
    LexicalGenerationReader,
    PrecomputedQueryEmbedding,
    QwenRuntimeClient,
    QwenRuntimeBusy,
    QwenRuntimeError,
    SourceDocument,
    SourceLink,
    SourceRepresentation,
    activate_generation,
    bootstrap_generation,
    canonical_json,
    canonical_url,
    chunk_document,
    compute_manifest_hash,
    load_manifest,
    model_revision_fingerprint,
    parse_archive_messages,
    read_pointer,
    resolve_current_generation,
    rollback_generation,
    rollback_reasons,
    sha256_file,
    sha256_text,
    split_archive_message_sessions,
    verify_generation,
)


class FakeCounter:
    def count(self, text: str) -> int:
        return max(1, len(text.split()))

    def windows(
        self,
        text: str,
        *,
        target_tokens: int = 650,
        hard_tokens: int = 800,
        overlap_tokens: int = 65,
    ) -> list[str]:
        del target_tokens, hard_tokens, overlap_tokens
        return [text.strip()] if text.strip() else []


class FakeClient:
    embedding_model = "embed-test"
    embedding_revision = "revision-test"
    reranker_model = "rerank-test"
    reranker_revision = "reranker-revision-test"
    dimension = 3
    batch_size = 2

    def __init__(self, *, reverse_rerank: bool = False, partial_rerank: bool = False):
        self.reverse_rerank = reverse_rerank
        self.partial_rerank = partial_rerank
        self.fail_next_query = False
        self.embed_timeouts: list[float] = []
        self.rerank_timeouts: list[float] = []
        self.rerank_calls = 0

    def health(self) -> dict[str, object]:
        return {"ready": True, "embedding": {"ready": True}}

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        if "axis-x" in text or "inactive-vector" in text:
            return np.asarray([100.0, 0.0, 0.0], dtype=np.float32)
        if "axis-y" in text:
            return np.asarray([0.0, 7.0, 0.0], dtype=np.float32)
        return np.asarray([0.0, 0.0, 3.0], dtype=np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vector(text) for text in texts])

    def embed_queries(self, texts: list[str], *, timeout: float | None = None) -> np.ndarray:
        assert timeout is not None
        self.embed_timeouts.append(timeout)
        if self.fail_next_query:
            self.fail_next_query = False
            raise QwenRuntimeError("transient query failure")
        return np.stack([self._vector(text + " axis-x") for text in texts])

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        timeout: float | None = None,
        document_token_counts: list[int] | None = None,
    ) -> list[dict[str, float | int]]:
        del query, document_token_counts
        assert timeout is not None
        self.rerank_timeouts.append(timeout)
        self.rerank_calls += 1
        indexes = list(range(min(top_n, len(documents))))
        if self.reverse_rerank:
            indexes.reverse()
        if self.partial_rerank and indexes:
            indexes.pop()
        return [
            {"index": index, "relevance_score": float(len(documents) - rank)}
            for rank, index in enumerate(indexes)
        ]


@pytest.fixture
def generation_config() -> GenerationConfig:
    return GenerationConfig(
        model_id="embed-test",
        model_revision="revision-test",
        dimension=3,
        reranker_model_id="rerank-test",
        reranker_revision="reranker-revision-test",
    )


def document(content_id: str, text: str, **kwargs: object) -> SourceDocument:
    return SourceDocument(
        content_id=content_id,
        source_kind="article",
        source_ref=f"source:{content_id}",
        text=text,
        title=f"title {content_id}",
        **kwargs,
    )


def build_generation(
    root: Path,
    generation_id: str,
    documents: list[SourceDocument],
    config: GenerationConfig,
    client: FakeClient | None = None,
    *,
    source_cursors: dict[str, dict[str, object]] | None = None,
) -> tuple[FakeClient, Path]:
    runtime = client or FakeClient()
    builder = GenerationBuilder(root, config, FakeCounter(), runtime)
    report = builder.build(
        documents,
        generation_id=generation_id,
        source_cursors=source_cursors,
    )
    assert report["ok"] is True
    return runtime, root / "generations" / generation_id


def wait_for_queue_size(client: QwenRuntimeClient, expected: int) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if client._runtime_queue._queue.qsize() == expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"queue size never reached {expected}: {client._runtime_queue._queue.qsize()}"
    )


def ledger_cursor(
    *members: str,
    schema: str,
    document_ids: tuple[str, ...] = (),
    delivery_ids: tuple[str, ...] = (),
    content_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    hashes = sorted(sha256_text(member) for member in members)
    cursor: dict[str, object] = {
        "mode": "set",
        "count": len(hashes),
        "set_hash": sha256_text(canonical_json(hashes)),
        "event_hashes": hashes,
        "schema": schema,
    }
    if document_ids:
        cursor["document_ids"] = sorted(document_ids)
    if delivery_ids:
        cursor["delivery_ids"] = sorted(delivery_ids)
    if content_ids:
        cursor["content_ids"] = sorted(content_ids)
    return cursor


def test_canonical_url_removes_tracking_parameters_without_corrupting_query() -> None:
    assert canonical_url("https://x/a?utm_source=y&foo=1") == "https://x/a?foo=1"
    assert canonical_url("https://x/a?foo=1&utm_medium=y&bar=2") == "https://x/a?foo=1&bar=2"
    assert canonical_url("https://x/a?foo=1&share_id=y#Frag") == "https://x/a?foo=1#Frag"
    assert canonical_url("http://X.example/A?from=feed#Case") == "https://X.example/A#Case"


def test_model_fingerprint_changes_after_same_size_preserved_mtime_rewrite(tmp_path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    weights = model / "weights.safetensors"
    weights.write_bytes(b"original-weight-bytes")
    before_stat = weights.stat()
    before = model_revision_fingerprint(model)

    weights.write_bytes(b"changed--weight-bytes")
    os.utime(weights, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after = model_revision_fingerprint(model)

    assert weights.stat().st_size == before_stat.st_size
    assert weights.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert after != before


def test_telegram_archive_chunks_real_format_by_gap_and_stable_member_identity() -> None:
    source_ref = "2026/08/25/telegram-Public.md"
    doc = SourceDocument(
        content_id=f"archive:{source_ref}",
        source_kind="telegram_archive",
        source_ref=source_ref,
        published_at="2026-08-25",
        text=(
            "# Telegram: Public\n\n"
            "> 导出 8 条消息\n\n"
            "[Telegram / Public / 09:00 / Alice] first\n\n"
            "[Telegram / Public / 09:01 / Bob] second\ncontinued detail\n\n"
            "[Telegram / Public / 09:02 / Carol] third\n\n"
            "[Telegram / Public / 09:03 / Dan] fourth\n\n"
            "[Telegram / Public / 09:04 / Eve] fifth\n\n"
            "[Telegram / Public / 09:05 / Frank] sixth\n\n"
            "[Telegram / Public / 09:16 / Grace] seventh\n\n"
            "[Telegram / Public / 09:17 / Heidi] eighth\n\n"
            "> 跳过空文本/低信息消息 0 条\n"
        ),
    )

    chunks = chunk_document(doc, FakeCounter())

    assert len(chunks) == 2
    assert len(chunks[0].member_ids) == 6
    assert len(chunks[1].member_ids) == 2
    assert chunks[0].member_ids[0].startswith("archive-message:v1:")
    assert chunks[0].member_ids[1].startswith("archive-message:v1:")
    assert "#L" not in chunks[0].member_ids[0]
    assert "continued detail" in chunks[0].text
    assert "跳过空文本" not in chunks[1].text
    assert chunks[0].end_time_ms is not None
    assert chunks[1].start_time_ms is not None
    assert chunks[1].start_time_ms - chunks[0].end_time_ms == 11 * 60 * 1000


def test_telegram_archive_chunks_at_twenty_with_two_message_overlap() -> None:
    source_ref = "2026/08/25/telegram-Busy.md"
    lines = [
        f"[Telegram / Busy / 10:{index:02d} / Sender {index}] message {index}"
        for index in range(23)
    ]
    doc = SourceDocument(
        content_id=f"archive:{source_ref}",
        source_kind="telegram_archive",
        source_ref=source_ref,
        published_at="2026-08-25",
        text="\n\n".join(lines),
    )

    chunks = chunk_document(doc, FakeCounter())

    assert [len(chunk.member_ids) for chunk in chunks] == [20, 5]
    assert chunks[1].member_ids[:2] == chunks[0].member_ids[-2:]


def test_archive_member_identity_does_not_depend_on_source_line_number() -> None:
    common = dict(
        content_id="archive:telegram-stable.md",
        source_kind="telegram_archive",
        source_ref="telegram-stable.md",
        published_at="2026-08-25",
    )
    original = SourceDocument(
        **common,
        text="[Telegram / Public / 09:00 / Alice] stable body",
    )
    shifted = SourceDocument(
        **common,
        text="# heading\n\n> metadata\n\n[Telegram / Public / 09:00 / Alice] stable body",
    )

    original_member = chunk_document(original, FakeCounter())[0].member_ids[0]
    shifted_member = chunk_document(shifted, FakeCounter())[0].member_ids[0]

    assert original_member == shifted_member


def test_archive_session_split_boundary_and_ordered_identity() -> None:
    source_ref = "2026/08/25/wechat-session.md"
    first_text = (
        "### 2026-08-25 09:00\n**Alice**: first\n\n"
        "### 2026-08-25 09:10\n**Bob**: second\n\n"
        "### 2026-08-25 09:21\n**Carol**: third\n"
    )
    reordered_text = (
        "### 2026-08-25 09:10\n**Bob**: second\n\n"
        "### 2026-08-25 09:00\n**Alice**: first\n\n"
        "### 2026-08-25 09:21\n**Carol**: third\n"
    )

    messages = parse_archive_messages(
        source_kind="wechat_archive",
        source_ref=source_ref,
        text=first_text,
    )
    sessions = split_archive_message_sessions(messages)
    reordered = parse_archive_messages(
        source_kind="wechat_archive",
        source_ref=source_ref,
        text=reordered_text,
    )

    assert [len(session) for session in sessions] == [2, 1]
    assert messages[0].member_id == reordered[1].member_id
    assert messages[1].member_id == reordered[0].member_id
    assert tuple(item.member_id for item in messages) != tuple(
        item.member_id for item in reordered
    )
    assert knowledge_index.CHUNKER_VERSION == "chatdaily-chunker-v3"


def test_archive_sessions_occupy_independent_search_slots_without_relaxing_content_cap(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    from chat_daily_tg.knowledge_sources import load_archive

    archive_root = tmp_path / "archive"
    day = archive_root / "2026" / "08" / "25"
    day.mkdir(parents=True)
    relative = "2026/08/25/telegram-public.md"
    (day / "telegram-public.md").write_text(
        "# Telegram\n\n"
        "[Telegram / Public / 09:00 / Alice] axis-x first session\n\n"
        "[Telegram / Public / 09:11 / Bob] axis-x second session\n",
        encoding="utf-8",
    )
    sessions, cursor = load_archive(archive_root)
    assert len(sessions) == 2
    article = SourceDocument(
        content_id="article:shared",
        source_kind="podcast_article",
        source_ref="https://example.com/shared",
        text="axis-x article primary",
        alternate_representations=(
            SourceRepresentation(
                text="axis-x article alternate",
                document_role="metadata",
                representation_type="vision_text",
            ),
        ),
    )
    client, generation_dir = build_generation(
        tmp_path / "index",
        "archive-session-search",
        [*sessions, article],
        generation_config,
        source_cursors={"archive": cursor},
    )

    reader = GenerationReader(
        generation_dir,
        client,
        FakeCounter(),
        expected=generation_config,
    )
    result = reader.search("axis-x", top_k=8, use_reranker=False)
    reader.close()

    hit_ids = [hit["content_id"] for hit in result["hits"]]
    session_ids = {document.content_id for document in sessions}
    assert session_ids.issubset(hit_ids)
    assert hit_ids.count("article:shared") == 1
    session_hits = [hit for hit in result["hits"] if hit["content_id"] in session_ids]
    assert {hit["source_ref"] for hit in session_hits} == {relative}
    assert all(not hit["source_links"] for hit in session_hits)


def test_one_content_preserves_distinct_text_representations() -> None:
    document = SourceDocument(
        content_id="shared-content",
        source_kind="podcast_article",
        source_ref="https://example.com/item",
        text="article body",
        document_role="original",
        representation_type="article",
        alternate_representations=(
            SourceRepresentation(
                text="caption and OCR facts",
                document_role="metadata",
                representation_type="vision_text",
                source_kind="podcast_article",
                source_ref="https://example.com/item",
                locator="gallery:key:vision_text",
            ),
        ),
    )

    chunks = chunk_document(document, FakeCounter())

    assert {chunk.content_id for chunk in chunks} == {"shared-content"}
    assert {chunk.representation_type for chunk in chunks} == {"article", "vision_text"}
    assert {chunk.document_role for chunk in chunks} == {"original", "metadata"}
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks) == 2
    assert all(chunk.locator.startswith("representation:") for chunk in chunks)


def test_conversation_chunk_locators_disambiguate_equal_timestamp_windows() -> None:
    source_ref = "2026/08/25/wechat-Busy.md"
    messages = []
    for index in range(23):
        messages.extend(
            (
                "### 2026-08-25 10:00",
                f"**Sender {index}**: repeated body",
                "",
            )
        )
    doc = SourceDocument(
        content_id=f"archive:{source_ref}",
        source_kind="wechat_archive",
        source_ref=source_ref,
        published_at="2026-08-25",
        text="\n".join(messages),
    )

    chunks = chunk_document(doc, FakeCounter())

    assert [len(chunk.member_ids) for chunk in chunks] == [20, 5]
    assert len({chunk.locator for chunk in chunks}) == len(chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert chunks[0].member_ids[0].startswith("archive-message:v1:")


@pytest.mark.parametrize(
    "generation_id",
    ["/absolute", "../escape", "a/b", r"a\b", ".", "..", ".hidden", "bad id"],
)
def test_builder_rejects_unsafe_generation_ids(
    tmp_path: Path, generation_config: GenerationConfig, generation_id: str
) -> None:
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), FakeClient())
    with pytest.raises(ValueError, match="invalid generation id"):
        builder.build([document("doc", "axis-x")], generation_id=generation_id)


def test_generation_symlink_and_manifest_directory_mismatch_are_rejected(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    generations = tmp_path / "generations"
    generations.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (generations / "linked").symlink_to(outside, target_is_directory=True)
    report = verify_generation(generations / "linked", expected=generation_config)
    assert report["ok"] is False
    assert "symlink" in report["errors"][0]

    _, generation_dir = build_generation(
        tmp_path, "safe-generation", [document("doc", "axis-x")], generation_config
    )
    manifest_path = generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generation_id"] = "different-generation"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="basename"):
        load_manifest(generation_dir)


def test_runtime_requires_embedding_and_reranker_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-model-contract.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_model="rerank-test",
        reranker_revision="rerank-rev",
        dimension=3,
    )
    monkeypatch.setattr(
        client,
        "_post_retry",
        lambda *args, **kwargs: {
            "model": "wrong-model",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        },
    )
    with pytest.raises(QwenRuntimeError, match="embedding response model mismatch"):
        client.embed_queries(["query"], timeout=1.0)

    monkeypatch.setattr(
        client,
        "_post_retry",
        lambda *args, **kwargs: {
            "model": "wrong-model",
            "results": [{"index": 0, "relevance_score": 1.0}],
        },
    )
    with pytest.raises(QwenRuntimeError, match="reranker response model mismatch"):
        client.rerank("query", ["document"], top_n=1, timeout=1.0)


@pytest.mark.parametrize(
    ("operation", "revision", "error"),
    [
        ("embedding", None, "embedding response revision attestation is missing"),
        ("embedding", "wrong-revision", "embedding response revision mismatch"),
        ("reranker", None, "reranker response revision attestation is missing"),
        ("reranker", "wrong-revision", "reranker response revision mismatch"),
    ],
)
def test_runtime_rejects_missing_or_mismatched_response_revision_attestation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    revision: str | None,
    error: str,
) -> None:
    client = QwenRuntimeClient(
        f"http://runtime-revision-{operation}-{revision}.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_model="rerank-test",
        reranker_revision="rerank-rev",
        dimension=3,
    )
    if operation == "embedding":
        response: dict[str, object] = {
            "model": "embed-test",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }
    else:
        response = {
            "model": "rerank-test",
            "results": [{"index": 0, "relevance_score": 1.0}],
        }
    if revision is not None:
        response["revision"] = revision
    monkeypatch.setattr(client, "_post_retry", lambda *_args, **_kwargs: response)

    with pytest.raises(QwenRuntimeError, match=error):
        if operation == "embedding":
            client.embed_queries(["query"], timeout=1.0)
        else:
            client.rerank("query", ["document"], top_n=1, timeout=1.0)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("embedding_revision", None, "embedding health response revision attestation is missing"),
        ("embedding_revision", "wrong", "embedding health response revision mismatch"),
        ("reranker_revision", None, "reranker health response revision attestation is missing"),
        ("reranker_revision", "wrong", "reranker health response revision mismatch"),
    ],
)
def test_runtime_health_strictly_attests_both_revisions_from_fake_server(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | None,
    error: str,
) -> None:
    import chat_daily_tg.knowledge_index as module

    payload: dict[str, object] = {
        "ready": True,
        "embedding": {"ready": True},
        "reranker": {"ready": True},
        "embedding_revision": "embed-rev",
        "reranker_revision": "rerank-rev",
    }
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeServer:
        def __init__(self, **_kwargs: object):
            pass

        def __enter__(self) -> "FakeServer":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "Client", FakeServer)
    client = QwenRuntimeClient(
        f"http://runtime-health-{field}-{value}.test/v1",
        embedding_revision="embed-rev",
        reranker_revision="rerank-rev",
    )

    with pytest.raises(QwenRuntimeError, match=error):
        client.health()


def test_runtime_sends_reranker_query_and_documents_as_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-rerank-payload.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_model="rerank-test",
        reranker_revision="rerank-rev",
        dimension=3,
    )
    captured: dict[str, object] = {}

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        del kwargs
        captured.update(args[2])
        return {
            "model": "rerank-test",
            "revision": "rerank-rev",
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.1},
            ],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)

    client.rerank("plain query", ["first", "second"], top_n=2, timeout=1.0)

    assert captured["query"] == "plain query"
    assert captured["documents"] == ["first", "second"]


def test_runtime_reranker_buckets_by_token_length_and_globally_remaps_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-rerank-buckets.test/v1",
        embedding_revision="embed-rev",
        reranker_model="rerank-test",
        reranker_revision="rerank-rev",
        dimension=3,
        timeout=1.0,
    )
    documents = [f"doc-{index}" for index in range(14)]
    token_counts = [90, 10, 70, 20, 80, 30, 60, 40, 50, 100, 110, 120, 130, 140]
    payloads: list[dict[str, object]] = []
    request_timeouts: list[float] = []
    queue_deadlines: list[float] = []

    class ImmediateQueue:
        def submit(
            self, task: object, *, online: bool, deadline: float
        ) -> dict[str, object]:
            assert callable(task)
            assert online is True
            queue_deadlines.append(deadline)
            return task()

    client._runtime_queue = ImmediateQueue()  # type: ignore[assignment]

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        payload = args[2]
        assert isinstance(payload, dict)
        payloads.append(payload)
        request_timeouts.append(float(kwargs["timeout"]))
        time.sleep(0.002)
        batch = payload["documents"]
        assert isinstance(batch, list)
        return {
            "model": "rerank-test",
            "revision": "rerank-rev",
            "results": [
                {
                    "index": local_index,
                    "relevance_score": float(int(document.split("-")[1]) // 2),
                }
                for local_index, document in reversed(list(enumerate(batch)))
            ],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)

    results = client.rerank(
        "query",
        documents,
        top_n=len(documents),
        timeout=0.5,
        document_token_counts=token_counts,
    )

    assert [len(payload["documents"]) for payload in payloads] == [4, 4, 4, 2]
    assert [
        document
        for payload in payloads
        for document in payload["documents"]
    ] == [documents[index] for index in sorted(range(14), key=lambda i: (token_counts[i], i))]
    assert len(set(queue_deadlines)) == 1
    assert request_timeouts[0] > request_timeouts[1] > request_timeouts[2] > 0
    assert [row["index"] for row in results] == [12, 13, 10, 11, 8, 9, 6, 7, 4, 5, 2, 3, 0, 1]
    assert sorted(int(row["index"]) for row in results) == list(range(14))


@pytest.mark.parametrize(
    "token_counts",
    ([1], [1, 0], [1, -1], [1, True], [1, 1.5]),
)
def test_runtime_reranker_rejects_invalid_token_counts(
    token_counts: list[object],
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-rerank-token-validation.test/v1",
        embedding_revision="embed-rev",
        reranker_revision="rerank-rev",
    )

    with pytest.raises(ValueError, match="token counts"):
        client.rerank(
            "query",
            ["one", "two"],
            top_n=2,
            timeout=1.0,
            document_token_counts=token_counts,  # type: ignore[arg-type]
        )


def test_runtime_reranker_attests_revision_for_every_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-rerank-bucket-revision.test/v1",
        embedding_revision="embed-rev",
        reranker_model="rerank-test",
        reranker_revision="rerank-rev",
    )
    calls = 0

    def fake_post(*args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        payload = args[2]
        assert isinstance(payload, dict)
        documents = payload["documents"]
        assert isinstance(documents, list)
        return {
            "model": "rerank-test",
            "revision": "rerank-rev" if calls == 1 else "wrong-revision",
            "results": [
                {"index": index, "relevance_score": float(index)}
                for index in range(len(documents))
            ],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)

    with pytest.raises(QwenRuntimeError, match="reranker response revision mismatch"):
        client.rerank(
            "query",
            [f"doc-{index}" for index in range(7)],
            top_n=7,
            timeout=1.0,
            document_token_counts=list(range(1, 8)),
        )
    assert calls == 2


def test_shared_worker_prioritizes_online_query_between_offline_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-priority.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_revision="rerank-rev",
        dimension=3,
        batch_size=1,
        queue_capacity=4,
        timeout=2.0,
    )
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        payload = args[2]
        assert isinstance(payload, dict)
        text = payload["input"][0]["text"]
        order.append(text)
        if text == "offline-1":
            entered.set()
            assert release.wait(1.0)
        return {
            "model": "embed-test",
            "revision": "embed-rev",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)
    errors: list[BaseException] = []

    def run(callable_: object) -> None:
        try:
            assert callable(callable_)
            callable_()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run, args=(lambda: client.embed_documents(["offline-1"]),))
    second = threading.Thread(target=run, args=(lambda: client.embed_documents(["offline-2"]),))
    online = threading.Thread(
        target=run,
        args=(lambda: client.embed_queries(["online"], timeout=1.0),),
    )
    first.start()
    assert entered.wait(1.0)
    second.start()
    wait_for_queue_size(client, 1)
    online.start()
    wait_for_queue_size(client, 2)
    release.set()
    for thread in (first, second, online):
        thread.join(2.0)
        assert not thread.is_alive()
    assert errors == []
    assert order == ["offline-1", "online", "offline-2"]


def test_bounded_worker_fails_online_fast_but_offline_waits_for_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QwenRuntimeClient(
        "http://runtime-capacity.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_revision="rerank-rev",
        dimension=3,
        batch_size=1,
        queue_capacity=1,
        timeout=2.0,
    )
    entered = threading.Event()
    release = threading.Event()

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        del kwargs
        payload = args[2]
        assert isinstance(payload, dict)
        text = payload["input"][0]["text"]
        if text == "offline-1":
            entered.set()
            assert release.wait(1.0)
        return {
            "model": "embed-test",
            "revision": "embed-rev",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)
    errors: list[BaseException] = []

    def offline(text: str) -> None:
        try:
            client.embed_documents([text])
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=offline, args=("offline-1",))
    queued = threading.Thread(target=offline, args=("offline-2",))
    waiting = threading.Thread(target=offline, args=("offline-3",))
    first.start()
    assert entered.wait(1.0)
    queued.start()
    wait_for_queue_size(client, 1)

    started = time.monotonic()
    with pytest.raises(QwenRuntimeBusy, match="online inference queue is full"):
        client.embed_queries(["online"], timeout=1.0)
    assert time.monotonic() - started < 0.2

    waiting.start()
    assert waiting.is_alive()
    release.set()
    for thread in (first, queued, waiting):
        thread.join(2.0)
        assert not thread.is_alive()
    assert errors == []


def test_queue_wait_is_included_in_online_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    client = QwenRuntimeClient(
        "http://runtime-deadline.test/v1",
        embedding_model="embed-test",
        embedding_revision="embed-rev",
        reranker_revision="rerank-rev",
        dimension=3,
        batch_size=1,
        queue_capacity=2,
        timeout=2.0,
    )
    entered = threading.Event()
    release = threading.Event()

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        del kwargs
        payload = args[2]
        assert isinstance(payload, dict)
        text = payload["input"][0]["text"]
        if text == "offline":
            entered.set()
            assert release.wait(1.0)
        return {
            "model": "embed-test",
            "revision": "embed-rev",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)
    offline = threading.Thread(target=lambda: client.embed_documents(["offline"]))
    offline.start()
    assert entered.wait(1.0)
    started = time.monotonic()
    with pytest.raises(QwenRuntimeBusy, match="deadline exhausted in queue"):
        client.embed_queries(["online"], timeout=0.05)
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.2
    release.set()
    offline.join(2.0)
    assert not offline.is_alive()


def test_queue_contention_returns_trusted_lexical_hits_before_online_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generation_config: GenerationConfig,
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "queue-lexical-fallback",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    client = QwenRuntimeClient(
        "http://runtime-query-contention.test/v1",
        embedding_model="embed-test",
        embedding_revision="revision-test",
        reranker_model="rerank-test",
        reranker_revision="reranker-revision-test",
        dimension=3,
        batch_size=1,
        queue_capacity=2,
        timeout=2.0,
    )
    entered = threading.Event()
    release = threading.Event()

    def fake_post(*args: object, **kwargs: object) -> dict[str, object]:
        del kwargs
        payload = args[2]
        assert isinstance(payload, dict)
        text = payload["input"][0]["text"]
        if text == "offline":
            entered.set()
            assert release.wait(1.0)
        return {
            "model": "embed-test",
            "revision": "revision-test",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }

    monkeypatch.setattr(client, "_post_retry", fake_post)
    offline = threading.Thread(target=lambda: client.embed_documents(["offline"]))
    offline.start()
    assert entered.wait(1.0)
    reader = GenerationReader(
        generation_dir,
        client,
        FakeCounter(),
        expected=generation_config,
    )
    started = time.monotonic()
    result = reader.search("deadline phrase", timeout=0.08)
    elapsed = time.monotonic() - started
    reader.close()
    release.set()
    offline.join(2.0)

    assert not offline.is_alive()
    assert elapsed < 0.25
    assert result["hits"][0]["content_id"] == "doc"
    assert any(
        reason.startswith("dense_query_failed:QwenRuntimeBusy")
        for reason in result["degraded_reasons"]
    )


def test_unreachable_runtime_preserves_lexical_results(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "unreachable-runtime",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    client = QwenRuntimeClient(
        "http://127.0.0.1:1/v1",
        embedding_model="embed-test",
        embedding_revision="revision-test",
        reranker_model="rerank-test",
        reranker_revision="reranker-revision-test",
        dimension=3,
        batch_size=1,
        timeout=0.15,
    )
    reader = GenerationReader(
        generation_dir,
        client,
        FakeCounter(),
        expected=generation_config,
    )
    started = time.monotonic()
    result = reader.search("deadline phrase", timeout=0.2)
    elapsed = time.monotonic() - started
    reader.close()

    assert elapsed < 0.6
    assert result["hits"][0]["content_id"] == "doc"
    assert any(
        reason.startswith("dense_query_failed:")
        for reason in result["degraded_reasons"]
    )


def test_reranker_revision_is_part_of_manifest_catalog_and_expected_context(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "reranker-revision",
        [document("doc", "axis-x")],
        generation_config,
    )
    manifest = load_manifest(generation_dir)
    assert manifest["reranker_revision"] == "reranker-revision-test"
    conn = sqlite3.connect(generation_dir / "catalog.sqlite")
    assert (
        conn.execute("SELECT reranker_revision FROM embedding_generations").fetchone()[0]
        == "reranker-revision-test"
    )
    conn.close()

    mismatched = replace(generation_config, reranker_revision="other-revision")
    report = verify_generation(generation_dir, expected=mismatched, full=True)
    assert "reranker_revision_mismatch" in report["errors"]
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=mismatched)
    assert reader.dense_enabled is False
    result = reader.search("axis-x")
    assert result["hits"][0]["content_id"] == "doc"
    assert result["reranker_used"] is False
    assert client.rerank_calls == 0
    reader.close()

    client.embedding_revision = "other-embedding-revision"
    reader = GenerationReader(
        generation_dir, client, FakeCounter(), expected=generation_config
    )
    assert reader.dense_enabled is False
    result = reader.search("axis-x")
    assert result["reranker_used"] is False
    assert "query_model_revision_mismatch" in result["degraded_reasons"]
    reader.close()
    with pytest.raises(ValueError, match="cannot activate invalid generation"):
        activate_generation(tmp_path, "reranker-revision", expected=mismatched)


def test_lexical_reader_fail_open_requires_parseable_manifest_and_sealed_catalog(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "lexical-fail-open",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    manifest_path = generation_dir / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    manifest.pop("query_template")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    reader = LexicalGenerationReader(
        generation_dir,
        degraded_reasons=("reader_context_invalid",),
    )
    result = reader.search("deadline phrase", use_reranker=True, use_dense=True)
    reader.close()

    assert result["hits"][0]["content_id"] == "doc"
    assert result["dense_enabled"] is False
    assert result["reranker_used"] is False
    assert result["degraded"] is True
    assert "manifest_hash_mismatch" in result["degraded_reasons"]
    assert "reader_context_invalid" in result["degraded_reasons"]
    assert "lexical_only" in result["degraded_reasons"]

    manifest_path.write_text("{malformed", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read generation manifest"):
        LexicalGenerationReader(generation_dir)

    manifest_path.write_bytes(original_manifest)
    with (generation_dir / "catalog.sqlite").open("ab") as handle:
        handle.write(b"catalog-tamper")
    with pytest.raises(ValueError, match="catalog seal mismatch"):
        LexicalGenerationReader(generation_dir)
    with pytest.raises(ValueError, match="trusted-reader checks"):
        GenerationReader(
            generation_dir,
            FakeClient(),
            FakeCounter(),
            expected=generation_config,
        )


@pytest.mark.parametrize("reader_kind", ["dense", "lexical"])
def test_online_readers_use_lightweight_verification_without_full_catalog_scans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generation_config: GenerationConfig,
    reader_kind: str,
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "catalog-only-vector-check",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    original_sha256_file = knowledge_index.sha256_file
    original_connect = knowledge_index.sqlite3.connect
    hashed: list[Path] = []
    statements: list[str] = []

    def tracked_sha256_file(path: Path, **kwargs: object) -> str:
        hashed.append(path)
        return original_sha256_file(path, **kwargs)

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(knowledge_index, "sha256_file", tracked_sha256_file)
    monkeypatch.setattr(knowledge_index.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(
        knowledge_index,
        "verify_generation",
        lambda *_args, **_kwargs: pytest.fail("online reader called offline verifier"),
    )
    if reader_kind == "dense":
        reader = GenerationReader(
            generation_dir, FakeClient(), FakeCounter(), expected=generation_config
        )
    else:
        reader = LexicalGenerationReader(generation_dir)
    result = reader.search("deadline phrase", use_dense=True, use_reranker=True)
    reader.close()

    assert sorted(hashed) == sorted([
        generation_dir / "catalog.sqlite",
        generation_dir / "vectors.f32",
    ])
    normalized = [" ".join(statement.casefold().split()) for statement in statements]
    assert not any("pragma integrity_check" in statement for statement in normalized)
    assert not any("pragma foreign_key_check" in statement for statement in normalized)
    assert not any("orphan_" in statement for statement in normalized)
    assert result["hits"][0]["content_id"] == "doc"
    assert result["dense_enabled"] is (reader_kind == "dense")


def test_online_reader_hashes_vectors_and_degrades_on_vector_tamper(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "online-vector-check",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    vectors = generation_dir / "vectors.f32"
    vectors.write_bytes(vectors.read_bytes()[:-4])

    reader = LexicalGenerationReader(generation_dir)
    result = reader.search("deadline phrase", use_dense=True, use_reranker=True)
    reader.close()

    assert result["hits"][0]["content_id"] == "doc"
    assert result["dense_enabled"] is False
    assert result["reranker_used"] is False
    assert "vectors_hash_mismatch" in result["degraded_reasons"]
    assert "vector_blob_length_mismatch" in result["degraded_reasons"]


def test_online_parallel_seals_classify_one_artifact_hash_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generation_config: GenerationConfig,
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "parallel-seal-error",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    original_sha256_file = knowledge_index.sha256_file
    hashed: list[Path] = []

    def failing_catalog_hash(path: Path, **kwargs: object) -> str:
        hashed.append(path)
        if path.name == "catalog.sqlite":
            raise OSError("catalog read failed")
        return original_sha256_file(path, **kwargs)

    monkeypatch.setattr(knowledge_index, "sha256_file", failing_catalog_hash)

    with pytest.raises(ValueError, match="catalog_hash_error:OSError"):
        GenerationReader(
            generation_dir, FakeClient(), FakeCounter(), expected=generation_config
        )
    assert sorted(path.name for path in hashed) == ["catalog.sqlite", "vectors.f32"]


def test_online_artifact_hash_detects_inflight_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"sealed")
    original_stat = knowledge_index.os.stat
    before = original_stat(artifact, follow_symlinks=False)
    calls = 0

    def changed_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal calls
        if Path(path) != artifact:
            return original_stat(path, follow_symlinks=follow_symlinks)
        calls += 1
        if calls == 1:
            return before
        values = list(before)
        values[9] = before.st_ctime + 1.0
        return os.stat_result(values)

    monkeypatch.setattr(knowledge_index.os, "stat", changed_stat)

    _size, _after, errors = knowledge_index._hash_online_artifact(
        artifact,
        sealed_hash=sha256_file(artifact),
        label="vectors",
    )

    assert errors == ["vectors_changed_during_verification"]


@pytest.mark.parametrize(
    ("statement", "expected_error", "foreign_key_error", "reader_rejects"),
    [
        (
            "INSERT INTO source_links VALUES "
            "('missing-content',1,NULL,1,NULL,'test-ledger',1)",
            "orphan_source_links",
            True,
            True,
        ),
        (
            "INSERT INTO assets VALUES "
            "('orphan-asset','missing-content','sha','phash','image/png',"
            "1,1,'local','ocr','{}',1)",
            "orphan_assets",
            True,
            False,
        ),
        (
            "INSERT INTO chunks VALUES "
            "('orphan-chunk','missing-content','loc',0,'text','hash','text',"
            "'raw','original',NULL,NULL,'[]','chunker','rendered',1)",
            "orphan_chunks",
            True,
            True,
        ),
        (
            "INSERT INTO exact_terms VALUES "
            "('orphan-term','content_id','missing-content',NULL)",
            "orphan_exact_content",
            True,
            False,
        ),
        (
            "INSERT INTO chunk_fts VALUES ('missing-chunk','text','title','source')",
            "orphan_fts",
            False,
            True,
        ),
    ],
)
def test_full_verifier_and_reader_reject_catalog_orphans(
    tmp_path: Path,
    generation_config: GenerationConfig,
    statement: str,
    expected_error: str,
    foreign_key_error: bool,
    reader_rejects: bool,
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "catalog-orphan-guard",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    catalog = generation_dir / "catalog.sqlite"
    connection = sqlite3.connect(catalog)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(statement)
    connection.commit()
    connection.close()
    manifest_path = generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["catalog_hash"] = sha256_file(catalog)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    report = verify_generation(generation_dir, expected=generation_config, full=True)

    assert report["ok"] is False
    assert expected_error in report["errors"]
    assert ("catalog_foreign_key_failed" in report["errors"]) is foreign_key_error
    if reader_rejects:
        with pytest.raises(ValueError, match="trusted-reader checks"):
            GenerationReader(
                generation_dir,
                FakeClient(),
                FakeCounter(),
                expected=generation_config,
            )
    else:
        reader = GenerationReader(
            generation_dir, FakeClient(), FakeCounter(), expected=generation_config
        )
        reader.close()


def test_full_verifier_and_reader_reject_catalog_generation_provenance_tamper(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "catalog-provenance-guard",
        [document("doc", "deadline phrase axis-x")],
        generation_config,
    )
    catalog = generation_dir / "catalog.sqlite"
    connection = sqlite3.connect(catalog)
    connection.execute(
        "UPDATE embedding_generations SET model_id='tampered-model'"
    )
    connection.commit()
    connection.close()
    manifest_path = generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["catalog_hash"] = sha256_file(catalog)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    report = verify_generation(generation_dir, expected=generation_config, full=True)

    assert report["ok"] is False
    assert "catalog_generation_context_mismatch" in report["errors"]
    with pytest.raises(ValueError, match="trusted-reader checks"):
        GenerationReader(
            generation_dir,
            FakeClient(),
            FakeCounter(),
            expected=generation_config,
        )


def test_embeddings_are_normalized_before_storage_and_dot_product_search(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "normalized",
        [document("x", "axis-x"), document("y", "axis-y")],
        generation_config,
    )
    matrix = np.memmap(generation_dir / "vectors.f32", dtype="<f4", mode="r", shape=(2, 3))
    assert np.linalg.norm(matrix, axis=1) == pytest.approx([1.0, 1.0], abs=1e-6)
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    result = reader.search("semantic query", use_reranker=False)
    reader.close()
    assert result["hits"][0]["content_id"] == "x"


def test_resume_snapshot_covers_all_source_provenance(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    base = document(
        "resume-doc",
        "axis-x",
        canonical_url="https://example.com/a?utm_source=test&keep=1",
        authority="ledger",
        mapping_status="confirmed",
        metadata={"topic": "one"},
        source_links=[SourceLink(chat_id=1, message_id=2, ledger_schema="sent.v1")],
        assets=[AssetRecord(asset_id="asset-1", sha256="abc")],
    )
    client, _ = build_generation(tmp_path, "resume-generation", [base], generation_config)
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), client)
    variants = [
        replace(base, authority="archive"),
        replace(base, mapping_status="pending"),
        replace(base, metadata={"topic": "two"}),
        replace(
            base,
            source_links=[SourceLink(chat_id=1, message_id=3, ledger_schema="sent.v1")],
        ),
        replace(base, assets=[AssetRecord(asset_id="asset-1", sha256="changed")]),
    ]
    for changed in variants:
        with pytest.raises(ValueError, match="source (snapshot|provenance) changed"):
            builder.build([changed], generation_id="resume-generation", resume=True)

    progress = json.loads(
        (tmp_path / "generations/resume-generation/build-progress.json").read_text(encoding="utf-8")
    )
    provenance = progress["source_provenance"]["documents"][0]
    assert provenance["authority"] == "ledger"
    assert provenance["source_links"][0]["ledger_schema"] == "sent.v1"
    assert provenance["assets"][0]["sha256"] == "abc"


def test_candidate_records_current_baseline_and_only_falls_back_when_current_missing(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(tmp_path, "baseline-one", [document("one", "axis-x")], generation_config)
    activate_generation(tmp_path, "baseline-one", expected=generation_config)
    build_generation(tmp_path, "baseline-two", [document("two", "axis-y")], generation_config)
    activate_generation(tmp_path, "baseline-two", expected=generation_config)

    _, candidate = build_generation(
        tmp_path,
        "candidate-current",
        [document("candidate", "axis-x candidate")],
        generation_config,
    )
    manifest = load_manifest(candidate)
    assert manifest["baseline_generation_id"] == "baseline-two"
    assert manifest["baseline_pointer"] == "CURRENT"
    progress = json.loads((candidate / "build-progress.json").read_text(encoding="utf-8"))
    assert progress["baseline_generation_id"] == "baseline-two"
    assert progress["baseline_pointer"] == "CURRENT"
    conn = sqlite3.connect(candidate / "catalog.sqlite")
    assert conn.execute(
        "SELECT baseline_generation_id,baseline_pointer FROM embedding_generations"
    ).fetchone() == ("baseline-two", "CURRENT")
    conn.close()

    (tmp_path / "CURRENT").unlink()
    _, fallback = build_generation(
        tmp_path,
        "candidate-previous",
        [document("candidate", "axis-x candidate")],
        generation_config,
    )
    fallback_manifest = load_manifest(fallback)
    assert fallback_manifest["baseline_generation_id"] == "baseline-one"
    assert fallback_manifest["baseline_pointer"] == "PREVIOUS"


def test_incremental_baseline_reconciles_but_reembeds_every_output_chunk(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, baseline = build_generation(
        tmp_path,
        "shadow-candidate",
        [document("unchanged", "axis-x"), document("changed", "old text")],
        generation_config,
    )
    baseline_hashes = {
        name: sha256_file(baseline / name)
        for name in ("manifest.json", "catalog.sqlite", "vectors.f32")
    }

    class CountingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.document_payloads: list[str] = []

        def embed_documents(self, texts: list[str]) -> np.ndarray:
            self.document_payloads.extend(texts)
            return super().embed_documents(texts)

    client = CountingClient()
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), client)
    report = builder.build(
        [document("unchanged", "axis-x"), document("changed", "axis-y new text")],
        generation_id="incremental-output",
        baseline_generation_id="shadow-candidate",
        baseline_pointer="INCREMENTAL",
    )

    output = tmp_path / "generations" / "incremental-output"
    manifest = load_manifest(output)
    assert report["ok"] is True
    assert manifest["baseline_generation_id"] == "shadow-candidate"
    assert manifest["baseline_pointer"] == "INCREMENTAL"
    assert len(client.document_payloads) == report["stats"]["row_count"] == 2
    assert any("axis-x" in payload for payload in client.document_payloads)
    assert any("axis-y new text" in payload for payload in client.document_payloads)
    assert verify_generation(output, expected=generation_config, full=True)["ok"] is True
    assert {
        name: sha256_file(baseline / name)
        for name in ("manifest.json", "catalog.sqlite", "vectors.f32")
    } == baseline_hashes


def test_incremental_baseline_provenance_is_paired_and_fixed_on_resume(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(
        tmp_path,
        "shadow-candidate",
        [document("one", "axis-x")],
        generation_config,
    )
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), FakeClient())

    with pytest.raises(ValueError, match="must be paired"):
        builder.build(
            [document("one", "axis-x")],
            generation_id="missing-pointer",
            baseline_generation_id="shadow-candidate",
        )

    builder.build(
        [document("one", "axis-x")],
        generation_id="incremental-output",
        baseline_generation_id="shadow-candidate",
        baseline_pointer="INCREMENTAL",
    )
    with pytest.raises(ValueError, match="baseline provenance"):
        builder.build(
            [document("one", "axis-x")],
            generation_id="incremental-output",
            resume=True,
            baseline_generation_id="different-candidate",
            baseline_pointer="INCREMENTAL",
        )


def test_incremental_baseline_requires_full_verified_matching_artifact(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, baseline = build_generation(
        tmp_path,
        "shadow-candidate",
        [document("one", "axis-x")],
        generation_config,
    )
    (baseline / "vectors.f32").write_bytes(b"corrupt")
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), FakeClient())

    with pytest.raises(ValueError, match="baseline generation is invalid"):
        builder.build(
            [document("one", "axis-x")],
            generation_id="incremental-output",
            baseline_generation_id="shadow-candidate",
            baseline_pointer="INCREMENTAL",
        )


@pytest.mark.parametrize(
    ("source_name", "baseline_cursor", "current_cursor", "message"),
    [
        (
            "sent_content_ledger",
            ledger_cursor("row-a", "row-b", schema="sent-content.v1"),
            ledger_cursor("row-a", schema="sent-content.v1"),
            "truncated or replaced",
        ),
        (
            "sent_content_ledger",
            ledger_cursor("row-a", "row-b", schema="sent-content.v1"),
            ledger_cursor("row-a", "row-c", schema="sent-content.v1"),
            "truncated or replaced",
        ),
        (
            "media_sent_ledger",
            ledger_cursor("row-a", schema="media-sent.legacy-v1"),
            ledger_cursor("row-a", schema="media-sent.v1"),
            "schema changed",
        ),
        (
            "media_sent_ledger",
            ledger_cursor(
                "row-a",
                schema="media-sent.legacy-v1",
                content_ids=("youtube:a",),
            ),
            {
                **ledger_cursor("row-a", schema="media-sent.legacy-v1"),
                "content_ids": [],
            },
            "content_ids inventory shrank",
        ),
    ],
)
def test_candidate_fails_closed_when_ledger_inventory_shrinks_or_changes(
    tmp_path: Path,
    generation_config: GenerationConfig,
    source_name: str,
    baseline_cursor: dict[str, object],
    current_cursor: dict[str, object],
    message: str,
) -> None:
    doc = document("stable", "axis-x stable")
    build_generation(
        tmp_path,
        "ledger-baseline",
        [doc],
        generation_config,
        source_cursors={source_name: baseline_cursor},
    )
    activate_generation(tmp_path, "ledger-baseline", expected=generation_config)
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), FakeClient())

    with pytest.raises(ValueError, match=message):
        builder.build(
            [doc],
            generation_id="ledger-candidate",
            source_cursors={source_name: current_cursor},
        )


def test_candidate_tombstones_missing_source_documents_and_carries_them_forward(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    missing = [
        document("archive", "axis-x archive", authority="archive"),
        document("database", "axis-y database", authority="chat-daily.db"),
        document(
            "podcast",
            "podcast body",
            authority="Podcast4Bot",
            source_links=[
                SourceLink(
                    chat_id=-100123,
                    message_id=55,
                    ledger_schema="media-sent.legacy-v1",
                )
            ],
            assets=[AssetRecord(asset_id="cover", sha256="abc", active=True)],
        ),
    ]
    retained = document("retained", "axis-x retained", authority="archive")
    build_generation(
        tmp_path,
        "source-baseline",
        [*missing, retained],
        generation_config,
    )
    activate_generation(tmp_path, "source-baseline", expected=generation_config)

    _, candidate = build_generation(
        tmp_path,
        "source-candidate",
        [retained],
        generation_config,
    )
    conn = sqlite3.connect(candidate / "catalog.sqlite")
    assert conn.execute(
        "SELECT content_id,active FROM content_items ORDER BY content_id"
    ).fetchall() == [
        ("archive", 0),
        ("database", 0),
        ("podcast", 0),
        ("retained", 1),
    ]
    tombstone = json.loads(
        conn.execute(
            "SELECT metadata_json FROM content_items WHERE content_id='podcast'"
        ).fetchone()[0]
    )["knowledge_tombstone"]
    assert tombstone["baseline_generation_id"] == "source-baseline"
    assert conn.execute("SELECT active FROM chunks WHERE content_id='podcast'").fetchall() == [(0,)]
    assert conn.execute(
        "SELECT ledger_schema,confirmed FROM source_links WHERE content_id='podcast'"
    ).fetchall() == [("media-sent.legacy-v1", 1)]
    assert conn.execute(
        "SELECT sha256,active FROM assets WHERE content_id='podcast'"
    ).fetchall() == [("abc", 0)]
    conn.close()

    activate_generation(tmp_path, "source-candidate", expected=generation_config)
    _, next_generation = build_generation(
        tmp_path,
        "source-next",
        [retained],
        generation_config,
    )
    conn = sqlite3.connect(next_generation / "catalog.sqlite")
    assert conn.execute(
        "SELECT content_id FROM content_items WHERE active=0 ORDER BY content_id"
    ).fetchall() == [("archive",), ("database",), ("podcast",)]
    conn.close()


def test_candidate_rejects_disappeared_or_changed_immutable_ledger_content(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    ledger_document = document(
        "ledger-content",
        "axis-x immutable",
        authority="sent-content.v1",
    )
    cursor = ledger_cursor(
        "immutable-row",
        schema="sent-content.v1",
        document_ids=(ledger_document.content_id,),
    )
    build_generation(
        tmp_path,
        "immutable-baseline",
        [ledger_document],
        generation_config,
        source_cursors={"sent_content_ledger": cursor},
    )
    activate_generation(tmp_path, "immutable-baseline", expected=generation_config)
    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), FakeClient())

    with pytest.raises(ValueError, match="immutable ledger content disappeared"):
        builder.build(
            [],
            generation_id="immutable-missing",
            source_cursors={"sent_content_ledger": cursor},
        )
    with pytest.raises(ValueError, match="immutable ledger content changed"):
        builder.build(
            [replace(ledger_document, text="changed")],
            generation_id="immutable-changed",
            source_cursors={"sent_content_ledger": cursor},
        )


def test_resume_uses_manifest_baseline_even_after_live_current_changes(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(tmp_path, "resume-base-one", [document("one", "axis-x")], generation_config)
    activate_generation(tmp_path, "resume-base-one", expected=generation_config)
    candidate_input = [document("candidate", "axis-y candidate")]
    client, candidate = build_generation(
        tmp_path,
        "resume-fixed-candidate",
        candidate_input,
        generation_config,
    )
    build_generation(tmp_path, "resume-base-two", [document("two", "axis-y")], generation_config)
    activate_generation(tmp_path, "resume-base-two", expected=generation_config)

    builder = GenerationBuilder(tmp_path, generation_config, FakeCounter(), client)
    report = builder.build(
        candidate_input,
        generation_id="resume-fixed-candidate",
        resume=True,
    )

    assert report["ok"] is True
    manifest = load_manifest(candidate)
    assert manifest["baseline_generation_id"] == "resume-base-one"
    assert manifest["baseline_pointer"] == "CURRENT"
    progress = json.loads((candidate / "build-progress.json").read_text(encoding="utf-8"))
    assert progress["baseline_generation_id"] == "resume-base-one"


def test_tombstones_are_filtered_from_exact_fts_dense_and_rows(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "tombstones",
        [
            document("inactive-id", "inactive-vector hidden phrase", active=False),
            document("active-id", "axis-y visible phrase"),
        ],
        generation_config,
    )
    assert verify_generation(generation_dir, expected=generation_config, full=True)["ok"]
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    assert reader._exact("inactive-id", 40) == []
    assert reader._lexical("hidden phrase", 40) == []
    result = reader.search("axis-x", use_reranker=False)
    reader.close()
    assert [hit["content_id"] for hit in result["hits"]] == ["active-id"]


@pytest.mark.parametrize("has_inactive", [False, True])
def test_dense_lookup_uses_memmap_fast_path_only_for_all_contiguous_active_rows(
    tmp_path: Path,
    generation_config: GenerationConfig,
    has_inactive: bool,
) -> None:
    documents = [document("active-x", "axis-x"), document("active-y", "axis-y")]
    if has_inactive:
        documents.append(document("inactive", "inactive-vector", active=False))
    client, generation_dir = build_generation(
        tmp_path,
        "dense-active-gap" if has_inactive else "dense-all-contiguous",
        documents,
        generation_config,
    )
    reader = GenerationReader(
        generation_dir, client, FakeCounter(), expected=generation_config
    )

    class MatrixSpy:
        def __init__(self, matrix: np.ndarray):
            self.matrix = matrix
            self.direct_calls = 0
            self.fancy_calls = 0

        def __matmul__(self, vector: np.ndarray) -> np.ndarray:
            self.direct_calls += 1
            return self.matrix @ vector

        def __getitem__(self, indexes: np.ndarray) -> np.ndarray:
            self.fancy_calls += 1
            return self.matrix[indexes]

    spy = MatrixSpy(reader.matrix)
    reader.matrix = spy  # type: ignore[assignment]

    result = reader.search("axis-x", use_reranker=False)
    reader.close()

    assert result["hits"][0]["content_id"] == "active-x"
    assert spy.direct_calls == (0 if has_inactive else 1)
    assert spy.fancy_calls == (1 if has_inactive else 0)


def test_message_exact_terms_are_scoped_and_do_not_collide_across_chats(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "scoped-message-ids",
        [
            document(
                "chat-one",
                "axis-x",
                source_links=[SourceLink(chat_id=10, message_id=123, ledger_schema="sent.v1")],
            ),
            document(
                "chat-two",
                "axis-y",
                source_links=[SourceLink(chat_id=20, message_id=123, ledger_schema="sent.v1")],
            ),
        ],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    rows = reader._rows(reader._exact("10:123", 40))
    assert {row["content_id"] for row in rows.values()} == {"chat-one"}
    assert reader._exact("123", 40) == []
    rows = reader._rows(reader._exact("sent.v1:20:123", 40))
    assert {row["content_id"] for row in rows.values()} == {"chat-two"}
    reader.close()


def test_ledger_bundle_indexes_every_authoritative_source_message_id(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "source-message-bundle",
        [
            document(
                "bundle",
                "axis-x",
                metadata={"source_message_ids": [456, 457, 458]},
                source_links=[
                    SourceLink(
                        chat_id=10,
                        message_id=123,
                        source_message_id=456,
                        ledger_schema="sent.v1",
                    )
                ],
            )
        ],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)

    for source_message_id in (456, 457, 458):
        rows = reader._rows(reader._exact(str(source_message_id), 40))
        assert {row["content_id"] for row in rows.values()} == {"bundle"}
    members = json.loads(
        reader.conn.execute("SELECT member_ids_json FROM chunks").fetchone()[0]
    )
    hit = reader.search("457", use_reranker=False)["hits"][0]
    reader.close()

    assert members == ["source-message:456", "source-message:457", "source-message:458"]
    assert hit["member_ids"] == (
        "source-message:456",
        "source-message:457",
        "source-message:458",
    )


def test_reader_returns_only_confirmed_source_links(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "confirmed-links-only",
        [
            document(
                "linked-content",
                "axis-x",
                source_links=[
                    SourceLink(
                        chat_id=10,
                        message_id=123,
                        ledger_schema="sent.v1",
                        confirmed=True,
                    ),
                    SourceLink(
                        chat_id=20,
                        message_id=456,
                        ledger_schema="unconfirmed.v1",
                        confirmed=False,
                    ),
                ],
            )
        ],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)

    result = reader.search("linked-content", use_reranker=False)
    reader.close()

    assert result["hits"][0]["source_links"] == (
        {
            "chat_id": 10,
            "thread_id": None,
            "message_id": 123,
            "source_message_id": None,
            "ledger_schema": "sent.v1",
            "confirmed": 1,
        },
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    assert reader._exact("20:456", 40) == []
    reader.close()


@pytest.mark.parametrize(
    "model_identity",
    ["RTX 5090", "iPhone 17 Pro", "qwen3-vl-embedding-8b"],
)
def test_product_models_are_exact_identities(
    tmp_path: Path,
    generation_config: GenerationConfig,
    model_identity: str,
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "model-" + model_identity.casefold().replace(" ", "-").replace(".", "-"),
        [document("model-document", f"release notes for {model_identity}")],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)

    rows = reader._rows(reader._exact(model_identity, 40))
    reader.close()

    assert {row["content_id"] for row in rows.values()} == {"model-document"}


def test_model_metadata_identities_are_exact_without_body_mention(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "metadata-model",
        [
            document(
                "metadata-model-document",
                "generic product notes",
                metadata={"model_ids": ["Device-X900", "Engine-Pro-2"]},
            )
        ],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)

    rows = reader._rows(reader._exact("Device-X900", 40))
    reader.close()

    assert {row["content_id"] for row in rows.values()} == {"metadata-model-document"}


def test_exact_hit_stays_first_after_reranking(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client = FakeClient(reverse_rerank=True)
    _, generation_dir = build_generation(
        tmp_path,
        "pin-exact",
        [
            document("exact-id", "axis-y exact-id shared phrase"),
            document("other-id", "axis-x exact-id shared phrase"),
        ],
        generation_config,
        client,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    result = reader.search("exact-id")
    reader.close()
    assert result["reranker_used"] is True
    assert result["hits"][0]["content_id"] == "exact-id"


def test_partial_or_over_budget_rerank_falls_back_to_rrf(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client = FakeClient(partial_rerank=True)
    _, generation_dir = build_generation(
        tmp_path,
        "rerank-fallback",
        [document("x", "axis-x shared phrase"), document("y", "axis-y shared phrase")],
        generation_config,
        client,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    partial = reader.search("shared phrase")
    assert partial["reranker_used"] is False
    assert any(reason.startswith("rerank_failed") for reason in partial["degraded_reasons"])

    class OverflowCounter(FakeCounter):
        def count(self, text: str) -> int:
            return 1000 if text.startswith("[来源类型]") else 1

    before = client.rerank_calls
    reader.counter = OverflowCounter()
    overflow = reader.search("shared phrase")
    reader.close()
    assert overflow["reranker_used"] is False
    assert client.rerank_calls == before


def test_query_deadline_is_passed_and_transient_failure_does_not_poison_reader(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "query-deadline",
        [document("x", "axis-x deadline phrase")],
        generation_config,
    )
    reader = GenerationReader(generation_dir, client, FakeCounter(), expected=generation_config)
    client.fail_next_query = True
    failed = reader.search("deadline phrase")
    assert reader.dense_enabled is True
    assert reader.degraded_reasons == []
    assert any(reason.startswith("dense_query_failed") for reason in failed["degraded_reasons"])

    recovered = reader.search("deadline phrase")
    reader.close()
    assert not any(
        reason.startswith("dense_query_failed") for reason in recovered["degraded_reasons"]
    )
    assert all(0 < value <= 8.0 for value in client.embed_timeouts)
    assert all(0 < value <= 8.0 for value in client.rerank_timeouts)
    assert {
        "exact",
        "lexical",
        "query_embedding",
        "dense_lookup",
        "rerank",
        "lookup",
        "total",
    } <= recovered["timing_ms"].keys()


def _prepared_query(
    reader: GenerationReader,
    query: str,
    *,
    vector: np.ndarray | None,
    error_type: str | None = None,
) -> PrecomputedQueryEmbedding:
    rendered = knowledge_index.render_query(
        query, template=reader.manifest["query_template"]
    )
    return PrecomputedQueryEmbedding(
        query=query,
        rendered_query=rendered,
        generation_id=reader.manifest["generation_id"],
        manifest_hash=reader.manifest["manifest_hash"],
        model_id=reader.manifest["model_id"],
        model_revision=reader.manifest["model_revision"],
        dimension=reader.manifest["dimension"],
        query_template=reader.manifest["query_template"],
        vector=vector,
        elapsed_ms=23.5,
        error_type=error_type,
    )


def test_precomputed_query_vector_is_consumed_without_second_embedding(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "prepared-query-success",
        [document("doc", "axis-x prepared phrase")],
        generation_config,
    )
    reader = GenerationReader(
        generation_dir, client, FakeCounter(), expected=generation_config
    )
    prepared = _prepared_query(
        reader,
        "prepared phrase",
        vector=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
    )
    before = len(client.embed_timeouts)

    result = reader.search("prepared phrase", precomputed_query=prepared)
    reader.close()

    assert len(client.embed_timeouts) == before
    assert result["dense_enabled"] is True
    assert result["reranker_used"] is True
    assert result["degraded"] is False
    assert result["timing_ms"]["query_embedding"] == 23.5


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("error", "dense_query_failed:QwenRuntimeBusy"),
        ("revision", "dense_query_failed:QwenRuntimeError"),
        ("vector", "dense_query_failed:QwenRuntimeError"),
    ],
)
def test_invalid_or_failed_precomputed_query_never_reembeds_or_reranks(
    tmp_path: Path,
    generation_config: GenerationConfig,
    mutation: str,
    reason: str,
) -> None:
    client, generation_dir = build_generation(
        tmp_path,
        "prepared-query-" + mutation,
        [document("doc", "axis-x prepared failure phrase")],
        generation_config,
    )
    reader = GenerationReader(
        generation_dir, client, FakeCounter(), expected=generation_config
    )
    prepared = _prepared_query(
        reader,
        "prepared failure phrase",
        vector=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        error_type="QwenRuntimeBusy" if mutation == "error" else None,
    )
    if mutation == "revision":
        prepared = replace(prepared, model_revision="wrong-revision")
    elif mutation == "vector":
        prepared = replace(
            prepared,
            vector=np.asarray([float("nan"), 0.0, 0.0], dtype=np.float32),
        )
    embed_before = len(client.embed_timeouts)
    rerank_before = client.rerank_calls

    result = reader.search(
        "prepared failure phrase", precomputed_query=prepared
    )
    reader.close()

    assert len(client.embed_timeouts) == embed_before
    assert client.rerank_calls == rerank_before
    assert reason in result["degraded_reasons"]
    assert result["reranker_used"] is False
    assert result["hits"][0]["content_id"] == "doc"


def test_activation_and_rollback_recover_interrupted_pointer_switches(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(tmp_path, "generation-one", [document("one", "axis-x")], generation_config)
    build_generation(tmp_path, "generation-two", [document("two", "axis-y")], generation_config)
    activate_generation(tmp_path, "generation-one", expected=generation_config)

    journal_path = tmp_path / ".switch-journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "activate",
                "phase": "current_written",
                "old_current": "generation-one",
                "old_previous": None,
                "new_current": "generation-two",
                "new_previous": "generation-one",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "PREVIOUS").write_text("generation-one\n", encoding="utf-8")
    (tmp_path / "CURRENT").write_text("generation-two\n", encoding="utf-8")
    activated = activate_generation(tmp_path, "generation-two", expected=generation_config)
    assert activated["recovered"] is True
    assert read_pointer(tmp_path, "CURRENT") == "generation-two"
    assert read_pointer(tmp_path, "PREVIOUS") == "generation-one"
    assert not journal_path.exists()

    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "rollback",
                "phase": "current_written",
                "old_current": "generation-two",
                "old_previous": "generation-one",
                "new_current": "generation-one",
                "new_previous": "generation-two",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "PREVIOUS").write_text("generation-two\n", encoding="utf-8")
    (tmp_path / "CURRENT").write_text("generation-one\n", encoding="utf-8")
    rolled_back = rollback_generation(tmp_path)
    assert rolled_back["recovered"] is True
    assert read_pointer(tmp_path, "CURRENT") == "generation-one"
    assert read_pointer(tmp_path, "PREVIOUS") == "generation-two"
    assert not journal_path.exists()


def test_activation_and_rollback_allow_independently_valid_cross_context_generations(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    baseline_client = FakeClient()
    build_generation(
        tmp_path,
        "context-baseline",
        [document("baseline", "axis-x")],
        generation_config,
        baseline_client,
    )
    activate_generation(tmp_path, "context-baseline", expected=generation_config)

    # Simulate a fully coherent prior chunker generation.  Builders only emit
    # the current version, while release/rollback must remain compatible with
    # older self-consistent manifests.
    baseline_dir = tmp_path / "generations/context-baseline"
    baseline_manifest_path = baseline_dir / "manifest.json"
    baseline_manifest = json.loads(baseline_manifest_path.read_text(encoding="utf-8"))
    baseline_manifest["chunker_version"] = "chatdaily-chunker-v1"
    baseline_manifest["manifest_hash"] = compute_manifest_hash(baseline_manifest)
    baseline_catalog = baseline_dir / "catalog.sqlite"
    conn = sqlite3.connect(baseline_catalog)
    conn.execute(
        "UPDATE embedding_generations SET chunker_version=?,manifest_hash=?",
        ("chatdaily-chunker-v1", baseline_manifest["manifest_hash"]),
    )
    conn.commit()
    conn.close()
    baseline_manifest["catalog_hash"] = sha256_file(baseline_catalog)
    baseline_manifest_path.write_text(json.dumps(baseline_manifest), encoding="utf-8")
    assert verify_generation(baseline_dir, full=True)["ok"] is True

    candidate_config = replace(
        generation_config,
        model_revision="revision-next",
        reranker_revision="reranker-revision-next",
    )
    candidate_client = FakeClient()
    candidate_client.embedding_revision = candidate_config.model_revision
    candidate_client.reranker_revision = candidate_config.reranker_revision
    build_generation(
        tmp_path,
        "context-candidate",
        [document("candidate", "axis-y")],
        candidate_config,
        candidate_client,
    )

    activated = activate_generation(
        tmp_path,
        "context-candidate",
        expected=candidate_config,
        baseline_generation_id="context-baseline",
    )
    assert activated["previous"] == "context-baseline"
    assert read_pointer(tmp_path, "CURRENT") == "context-candidate"
    assert read_pointer(tmp_path, "PREVIOUS") == "context-baseline"
    assert verify_generation(
        tmp_path / "generations/context-baseline", full=True
    )["ok"] is True
    baseline_reader = GenerationReader(
        baseline_dir,
        baseline_client,
        FakeCounter(),
        expected=replace(generation_config, chunker_version="chatdaily-chunker-v1"),
    )
    assert baseline_reader.dense_enabled is True
    assert baseline_reader.search("axis-x", use_reranker=False)["generation_id"] == (
        "context-baseline"
    )
    baseline_reader.close()

    rolled_back = rollback_generation(tmp_path)
    assert rolled_back["generation_id"] == "context-baseline"
    assert read_pointer(tmp_path, "CURRENT") == "context-baseline"
    assert read_pointer(tmp_path, "PREVIOUS") == "context-candidate"


def test_activation_receipt_baseline_is_checked_inside_switch_lock(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(
        tmp_path,
        "receipt-baseline",
        [document("baseline", "axis-x")],
        generation_config,
    )
    build_generation(
        tmp_path,
        "receipt-candidate",
        [document("candidate", "axis-y")],
        generation_config,
    )
    activate_generation(tmp_path, "receipt-baseline", expected=generation_config)

    with pytest.raises(ValueError, match="receipt baseline no longer matches CURRENT"):
        activate_generation(
            tmp_path,
            "receipt-candidate",
            expected=generation_config,
            baseline_generation_id="stale-baseline",
        )
    assert read_pointer(tmp_path, "CURRENT") == "receipt-baseline"


def test_rollback_keeps_corrupt_failed_generation_and_activates_complete_previous(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(
        tmp_path,
        "safe-generation",
        [document("safe", "axis-x")],
        generation_config,
    )
    build_generation(
        tmp_path,
        "failed-generation",
        [document("failed", "axis-y")],
        generation_config,
    )
    activate_generation(tmp_path, "safe-generation", expected=generation_config)
    activate_generation(tmp_path, "failed-generation", expected=generation_config)
    failed_vectors = tmp_path / "generations/failed-generation/vectors.f32"
    failed_vectors.write_bytes(b"corrupt-vector-artifact")

    with pytest.raises(ValueError, match="CURRENT generation is invalid"):
        resolve_current_generation(tmp_path)
    result = rollback_generation(tmp_path)

    assert result["generation_id"] == "safe-generation"
    assert read_pointer(tmp_path, "CURRENT") == "safe-generation"
    assert read_pointer(tmp_path, "PREVIOUS") == "failed-generation"
    assert failed_vectors.read_bytes() == b"corrupt-vector-artifact"


@pytest.mark.parametrize(
    ("phase", "current", "previous"),
    [
        ("prepared", "generation-one", None),
        ("prepared", "generation-one", "generation-one"),
        ("previous_written", "generation-one", "generation-one"),
        ("previous_written", "generation-two", "generation-one"),
        ("current_written", "generation-two", "generation-one"),
        ("statuses_written", "generation-two", "generation-one"),
    ],
)
def test_resolve_current_recovers_every_activation_crash_point(
    tmp_path: Path,
    generation_config: GenerationConfig,
    phase: str,
    current: str,
    previous: str | None,
) -> None:
    build_generation(tmp_path, "generation-one", [document("one", "axis-x")], generation_config)
    build_generation(tmp_path, "generation-two", [document("two", "axis-y")], generation_config)
    activate_generation(tmp_path, "generation-one", expected=generation_config)
    journal_path = tmp_path / ".switch-journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "activate",
                "phase": phase,
                "old_current": "generation-one",
                "old_previous": None,
                "new_current": "generation-two",
                "new_previous": "generation-one",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "CURRENT").write_text(current + "\n", encoding="utf-8")
    if previous is None:
        (tmp_path / "PREVIOUS").unlink(missing_ok=True)
    else:
        (tmp_path / "PREVIOUS").write_text(previous + "\n", encoding="utf-8")
    if phase == "statuses_written":
        for generation_id, status in (
            ("generation-one", "retired"),
            ("generation-two", "active"),
        ):
            path = tmp_path / "generations" / generation_id / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["status"] = status
            path.write_text(json.dumps(manifest), encoding="utf-8")

    current_report = verify_generation(
        tmp_path / "generations" / current,
        expected=generation_config,
        full=True,
    )
    assert current_report["ok"] is True
    assert resolve_current_generation(tmp_path, expected=generation_config) == "generation-two"
    assert read_pointer(tmp_path, "CURRENT") == "generation-two"
    assert read_pointer(tmp_path, "PREVIOUS") == "generation-one"
    assert load_manifest(tmp_path / "generations/generation-two")["status"] == "active"
    assert load_manifest(tmp_path / "generations/generation-one")["status"] == "retired"
    assert not journal_path.exists()


def test_switch_publishes_previous_before_current(
    tmp_path: Path,
    generation_config: GenerationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_generation(tmp_path, "generation-one", [document("one", "axis-x")], generation_config)
    build_generation(tmp_path, "generation-two", [document("two", "axis-y")], generation_config)
    activate_generation(tmp_path, "generation-one", expected=generation_config)

    import chat_daily_tg.knowledge_index as module

    original = module.atomic_write_text
    pointer_writes: list[str] = []

    def recording_write(path: Path, text: str) -> None:
        if path.name in {"CURRENT", "PREVIOUS"}:
            pointer_writes.append(path.name)
        original(path, text)

    monkeypatch.setattr(module, "atomic_write_text", recording_write)
    activate_generation(tmp_path, "generation-two", expected=generation_config)
    assert pointer_writes[:2] == ["PREVIOUS", "CURRENT"]


def test_resolve_current_without_journal_does_not_mutate_generation_state(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "generation-one",
        [document("one", "axis-x")],
        generation_config,
    )
    activate_generation(tmp_path, "generation-one", expected=generation_config)
    before = {
        "current": (tmp_path / "CURRENT").read_bytes(),
        "manifest": (generation_dir / "manifest.json").read_bytes(),
        "previous_exists": (tmp_path / "PREVIOUS").exists(),
    }
    assert resolve_current_generation(tmp_path, expected=generation_config) == "generation-one"
    after = {
        "current": (tmp_path / "CURRENT").read_bytes(),
        "manifest": (generation_dir / "manifest.json").read_bytes(),
        "previous_exists": (tmp_path / "PREVIOUS").exists(),
    }
    assert after == before


def test_bootstrap_seeds_only_current_and_is_idempotent(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "baseline-one",
        [document("one", "axis-x")],
        generation_config,
    )
    manifest_before = (generation_dir / "manifest.json").read_bytes()

    first = bootstrap_generation(
        tmp_path, "baseline-one", expected=generation_config
    )
    second = bootstrap_generation(
        tmp_path, "baseline-one", expected=generation_config
    )

    assert first == {
        "status": "bootstrapped",
        "generation_id": "baseline-one",
        "previous": None,
        "idempotent": False,
        "production_authorized": False,
    }
    assert second["idempotent"] is True
    assert read_pointer(tmp_path, "CURRENT") == "baseline-one"
    assert not (tmp_path / "PREVIOUS").exists()
    assert not (tmp_path / ".switch-journal.json").exists()
    assert (generation_dir / "manifest.json").read_bytes() == manifest_before


def test_bootstrap_rejects_nonempty_or_unverified_pointer_state(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, first_dir = build_generation(
        tmp_path,
        "baseline-one",
        [document("one", "axis-x")],
        generation_config,
    )
    _, second_dir = build_generation(
        tmp_path,
        "baseline-two",
        [document("two", "axis-y")],
        generation_config,
    )
    bootstrap_generation(tmp_path, "baseline-one", expected=generation_config)

    with pytest.raises(ValueError, match="CURRENT to be absent"):
        bootstrap_generation(tmp_path, "baseline-two", expected=generation_config)

    (tmp_path / "CURRENT").unlink()
    (tmp_path / "PREVIOUS").write_text("baseline-one\n", encoding="utf-8")
    with pytest.raises(ValueError, match="PREVIOUS to be absent"):
        bootstrap_generation(tmp_path, "baseline-two", expected=generation_config)

    (tmp_path / "PREVIOUS").unlink()
    (second_dir / "vectors.f32").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cannot bootstrap invalid generation"):
        bootstrap_generation(tmp_path, "baseline-two", expected=generation_config)
    assert not (tmp_path / "CURRENT").exists()
    assert verify_generation(first_dir, full=True)["ok"] is True


def test_bootstrapped_baseline_becomes_previous_on_first_activation(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(
        tmp_path,
        "baseline-v1",
        [document("one", "axis-x")],
        generation_config,
    )
    build_generation(
        tmp_path,
        "candidate-v2",
        [document("two", "axis-y")],
        generation_config,
    )
    bootstrap_generation(tmp_path, "baseline-v1", expected=generation_config)

    result = activate_generation(
        tmp_path,
        "candidate-v2",
        expected=generation_config,
        baseline_generation_id="baseline-v1",
    )

    assert result["previous"] == "baseline-v1"
    assert read_pointer(tmp_path, "CURRENT") == "candidate-v2"
    assert read_pointer(tmp_path, "PREVIOUS") == "baseline-v1"


def test_current_equal_previous_is_rejected(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    build_generation(tmp_path, "generation-one", [document("one", "axis-x")], generation_config)
    activate_generation(tmp_path, "generation-one", expected=generation_config)
    (tmp_path / "PREVIOUS").write_text("generation-one\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not reference the same"):
        rollback_generation(tmp_path)


def test_verification_allows_inactive_chunks_but_checks_all_vector_and_fts_rows(
    tmp_path: Path, generation_config: GenerationConfig
) -> None:
    _, generation_dir = build_generation(
        tmp_path,
        "inactive-verification",
        [document("inactive", "axis-x", active=False), document("active", "axis-y")],
        generation_config,
    )
    report = verify_generation(generation_dir, expected=generation_config, full=True)
    assert report["ok"] is True
    assert report["stats"]["chunks"] == 2
    assert report["stats"]["active_chunks"] == 1

    conn = sqlite3.connect(generation_dir / "catalog.sqlite")
    assert conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM vector_rows").fetchone()[0] == 2
    conn.close()


def test_rollback_reasons_use_all_strict_snapshot_thresholds() -> None:
    metrics = {
        "generation": {"identity_valid": False, "manifest_valid": False},
        "verification": {"coverage": 0.98},
        "evaluation": {
            "recall_at_50": {"drop": 0.031},
            "stratum_ndcg_at_5": {"max_drop": 0.051},
        },
        "runtime": {
            "reranker": {"error_rate": 0.021},
            "p95_windows": [
                {"p95_ms": 8001.0},
                {"p95_ms": 8002.0},
                {"p95_ms": 8003.0},
            ],
        },
        "source_links": {"bad_count": 1},
    }

    assert rollback_reasons(metrics) == [
        "generation_identity_mismatch",
        "manifest_invalid",
        "coverage_below_99_percent",
        "recall_drop_over_3pp",
        "stratum_ndcg_drop_over_5pp",
        "reranker_error_rate_over_2_percent",
        "p95_over_8s_three_windows",
        "incorrect_source_link",
    ]

    metrics["generation"] = {"identity_valid": True, "manifest_valid": True}
    metrics["verification"]["coverage"] = 0.99
    metrics["evaluation"]["recall_at_50"]["drop"] = 0.03
    metrics["evaluation"]["stratum_ndcg_at_5"]["max_drop"] = 0.05
    metrics["runtime"]["reranker"]["error_rate"] = 0.02
    metrics["runtime"]["p95_windows"][1]["p95_ms"] = 8000.0
    metrics["source_links"]["bad_count"] = 0
    assert rollback_reasons(metrics) == []
