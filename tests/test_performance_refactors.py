"""State, result and work-count contracts for the performance refactor."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import importlib
import json
import math
from pathlib import Path
import random
from types import SimpleNamespace

import httpx
import pytest

from chat_daily_tg import content_seen, knowledge_index, sent_ledger
from chat_daily_tg.application import _export_wechat_lane
from chat_daily_tg.content_seen import ContentSeenStore
from chat_daily_tg.evidence_index import EmbeddingGeneration, EvidenceChunk, EvidenceIndex
from chat_daily_tg.growth_store import insert_segments
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.resource_util import enter_optional_client
from chat_daily_tg.vector_math import CosineScorer, cosine_similarity
from chat_daily_tg.wx_exporter import _download_wx_images, export_group
from tests.test_growth_store import _seg
from tests.test_knowledge_index import (
    FakeClient,
    FakeCounter,
    build_generation,
    document,
)
from tests.test_media_summarizer_model_routing import _config
from tests.test_wx_exporter import _cand


@pytest.fixture
def generation_config():
    return knowledge_index.GenerationConfig(
        model_id="embed-test",
        model_revision="revision-test",
        dimension=3,
        reranker_model_id="rerank-test",
        reranker_revision="reranker-revision-test",
    )


def legacy_cosine(left, right):
    if not left or len(left) != len(right):
        return 0.0
    dot = (
        math.sumprod if hasattr(math, "sumprod") else lambda a, b: sum(x * y for x, y in zip(a, b))
    )
    product = dot(left, right)
    a, b = math.sqrt(dot(left, left)), math.sqrt(dot(right, right))
    return product / (a * b) if a and b else 0.0


def test_cosine_snapshot_preserves_exact_scores_append_and_caller_mutation():
    rng = random.Random(472)
    vectors = [[rng.uniform(-1, 1) for _ in range(257)] for _ in range(35)]
    query = vectors[0][:]
    expected = [legacy_cosine(query, vector) for vector in vectors]
    scorer = CosineScorer(vectors)
    vectors[0][0] = 100.0
    assert scorer.similarities(query) == expected
    scorer.append(query)
    assert scorer.similarities(query) == expected + [legacy_cosine(query, query)]
    for left, right in [([], []), ([0.0], [1.0]), ([1.0, 0.0], [1.0]), ([1.0], [-1.0])]:
        assert cosine_similarity(left, right) == legacy_cosine(left, right)
        assert CosineScorer([right]).similarities(left) == [legacy_cosine(left, right)]


def test_evidence_search_matches_original_float_math_and_replace(tmp_path):
    rng = random.Random(940)
    generation = EmbeddingGeneration("perf", "model", "weights", 17, normalized=False)
    chunks = [EvidenceChunk(str(i), "group", str(i), "sender", f"text {i}") for i in range(40)]
    vectors = [[rng.random() for _ in range(17)] for _ in chunks]
    index = EvidenceIndex(tmp_path / "evidence.db", generation=generation)
    try:
        index.replace(chunks, vectors)
        query = vectors[3]
        index.search(query, top_k=4)  # Load the persisted float32 snapshot.
        expected = [(fields[0], legacy_cosine(query, vector)) for fields, vector in index._rows]
        expected.sort(key=lambda row: row[1], reverse=True)
        cutoff = expected[7][1]
        hits = index.search(query, top_k=8, min_similarity=cutoff)
        assert [(hit.source_id, hit.similarity) for hit in hits] == expected[:8]
        index.replace(chunks[:1], vectors[:1])
        assert [hit.source_id for hit in index.search(query, top_k=8)] == ["0"]
    finally:
        index.close()


def test_title_features_cache_keeps_live_rows_and_tie_order(tmp_path, monkeypatch):
    path = tmp_path / "titles.db"
    store, other = ContentSeenStore(path), ContentSeenStore(path)
    title = "公司发布全新旗舰模型和开发者价格调整计划"
    calls = []
    original = content_seen._char_bigrams
    monkeypatch.setattr(
        content_seen, "_char_bigrams", lambda value: (calls.append(value), original(value))[1]
    )
    try:
        store.register_title(title, "A", 1)
        assert store.find_similar_title(title).msg_id == 1
        assert store.find_similar_title(title).msg_id == 1
        assert len(calls) == 3  # Two query features, one immutable history feature.
        other.register_title(title, "B", 2)
        assert store.find_similar_title(title).msg_id == 2
        other._conn.execute("DELETE FROM title_seen WHERE msg_id=2")
        other._conn.commit()
        assert store.find_similar_title(title).msg_id == 1
        assert store.find_similar_title(title, max_scan=0) is None
    finally:
        store.close()
        other.close()


def test_growth_overlap_keeps_boundaries_cross_chat_and_same_batch(tmp_path):
    original = _seg("2026-09-01", 100, 200)
    encompassed = _seg("2026-09-02", 120, 150)
    touch = _seg("2026-09-02", 200, 300)
    cross_chat = replace(encompassed, id="other-chat", chat_id=8)
    inserted = insert_segments(tmp_path / "growth.db", [original, encompassed, touch, cross_chat])
    assert [seg.id for seg in inserted] == [original.id, touch.id, cross_chat.id]


def test_wx_all_cached_never_queries_attachments_and_keeps_order(tmp_path, monkeypatch):
    for mid in (1, 2):
        (tmp_path / f"{mid}.jpg").write_bytes(b"cached-jpeg")
    monkeypatch.setattr(
        "chat_daily_tg.wx_exporter._attachment_ids_by_local_id",
        lambda *_: pytest.fail("cached media queried CLI"),
    )
    decoded = []
    monkeypatch.setattr(
        "chat_daily_tg.wx_exporter._decode_wxgf_in_place", lambda path: decoded.append(path.name)
    )
    candidates = [_cand(2), _cand(1), _cand(3, score=0.0)]
    result = _download_wx_images(
        candidates, group_name="G", since="2026-09-01", until="2026-09-01", media_dir=tmp_path
    )
    assert [Path(item.local_path).name if item.local_path else None for item in result] == [
        "2.jpg",
        "1.jpg",
        None,
    ]
    assert sorted(decoded) == ["1.jpg", "2.jpg"]


def test_wx_mixed_cache_survives_attachment_failure_and_empty_work_is_noop(tmp_path, monkeypatch):
    (tmp_path / "1.jpg").write_bytes(b"cached-jpeg")
    monkeypatch.setattr("chat_daily_tg.wx_exporter._attachment_ids_by_local_id", lambda *_: {})
    result = _download_wx_images(
        [_cand(1), _cand(2)],
        group_name="G",
        since="2026-09-01",
        until="2026-09-01",
        media_dir=tmp_path,
    )
    assert result[0].local_path == str(tmp_path / "1.jpg")
    assert result[1].local_path is None
    blocked = tmp_path / "ordinary-file"
    blocked.write_text("not a directory")
    result = _download_wx_images(
        [_cand(3)], group_name="G", since="2026-09-01", until="2026-09-01", media_dir=blocked
    )
    assert result[0].local_path is None


def test_wx_download_disabled_keeps_export_and_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chat_daily_tg.wx_exporter.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="> 导出 1 条消息\n正文", stderr=""),
    )
    monkeypatch.setattr(
        "chat_daily_tg.wx_exporter.extract_wx_media_candidates", lambda *a, **k: [_cand(1)]
    )
    monkeypatch.setattr(
        "chat_daily_tg.wx_exporter._download_wx_images",
        lambda *a, **k: pytest.fail("disabled vision downloaded images"),
    )
    result = export_group(
        "G", "2026-09-01", "2026-09-01", tmp_path / "out.md", download_images=False
    )
    assert result.message_count == 1 and len(result.media_candidates) == 1
    assert "正文" in result.content and result.media_candidates[0].local_path is None


def test_wechat_lane_passes_disabled_vision_to_export(tmp_path, monkeypatch):
    cfg = _config("bilibili")
    cfg.models.vision.enabled = False
    cfg.sources.wechat.groups = ["G"]
    captured = []

    def export(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(content="正文", message_count=1, media_candidates=[])

    monkeypatch.setattr("chat_daily_tg.application.export_group", export)
    groups, _ = _export_wechat_lane(cfg, date_str="2026-09-01", archive_dir=tmp_path)
    assert groups and captured[0]["download_images"] is False






def test_fresh_generation_chunks_once_and_resume_revalidates(
    tmp_path, generation_config, monkeypatch
):
    calls = []
    original = knowledge_index.chunk_document_with_config

    def counted(item, counter, config):
        calls.append(item.content_id)
        return original(item, counter, config)

    monkeypatch.setattr(knowledge_index, "chunk_document_with_config", counted)
    docs = [document("a", "axis-x"), document("b", "axis-y")]
    builder = knowledge_index.GenerationBuilder(
        tmp_path, generation_config, FakeCounter(), FakeClient()
    )
    assert builder.build(docs, generation_id="chunk-once")["ok"]
    assert calls == ["a", "b"]
    calls.clear()
    assert builder.build(docs, generation_id="chunk-once", resume=True)["ok"]
    assert calls == ["a", "b"]
    with pytest.raises(ValueError, match="snapshot"):
        builder.build([document("a", "changed")], generation_id="chunk-once", resume=True)


def test_source_links_use_one_query_without_cross_content_mix(tmp_path, generation_config):
    docs = [
        document(
            f"item-{i}",
            "shared phrase",
            source_links=[
                knowledge_index.SourceLink(
                    chat_id=i + 1, thread_id=i + 10, message_id=7, ledger_schema="sent.v1"
                ),
                knowledge_index.SourceLink(
                    chat_id=i + 1, thread_id=i + 10, message_id=8, ledger_schema="sent.v1"
                ),
                knowledge_index.SourceLink(
                    chat_id=i + 1, message_id=9, ledger_schema="draft", confirmed=False
                ),
            ],
        )
        for i in range(5)
    ]
    client, folder = build_generation(tmp_path, "links", docs, generation_config)
    reader = knowledge_index.GenerationReader(
        folder, client, FakeCounter(), expected=generation_config
    )
    queries = []
    reader.conn.set_trace_callback(queries.append)
    try:
        hits = reader.search("shared phrase", top_k=5, use_dense=False)["hits"]
        assert len(hits) == 5
        assert len([q for q in queries if "FROM source_links" in q]) == 1
        for hit in hits:
            number = int(hit["content_id"].split("-")[1])
            assert [link["message_id"] for link in hit["source_links"]] == [7, 8]
            assert all(
                link["chat_id"] == number + 1 and link["thread_id"] == number + 10
                for link in hit["source_links"]
            )
            assert all("content_id" not in link for link in hit["source_links"])
    finally:
        reader.close()


@pytest.mark.parametrize("source", ["bilibili", "youtube"])
@pytest.mark.parametrize("vision", [False, True])
def test_digest_summary_pool_is_lazy_reused_and_closed(source, vision, tmp_path, monkeypatch):
    module = importlib.import_module(f"chat_daily_tg.{source}_digest")
    cfg = _config(source)
    cfg.models.vision.enabled = vision
    monkeypatch.setenv("K", "test-key")
    clients = []
    original = httpx.Client

    def factory(**kwargs):
        client = original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [{"finish_reason": "stop", "message": {"content": "summary"}}]
                    },
                )
            )
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    video = SimpleNamespace(title="test", description="description", bvid="b", video_id="y")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    with ExitStack() as resources:
        summarize = module.build_summarizer(cfg, stack=resources)
        assert clients == []
        for _ in range(3):
            assert summarize(video, cover if vision else None) == "summary"
        assert len(clients) == 1 and not clients[0].is_closed
    assert clients[0].is_closed


@pytest.mark.parametrize("source", ["bilibili", "youtube"])
def test_cover_pool_network_policy_and_owner_cleanup(source, tmp_path, monkeypatch):
    module = importlib.import_module(f"chat_daily_tg.{source}_digest")
    fixture = importlib.import_module(f"tests.test_{source}_digest")
    original = httpx.Client
    clients, options = [], []

    def factory(**kwargs):
        options.append(kwargs)
        client = original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"image"))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr(module, "append_message_ids", lambda ids, **kwargs: len(ids))
    cfg = fixture._cfg()
    sender = fixture.FakeSender()
    seen = SeenStore(tmp_path / "seen")
    videos = [fixture._video(), fixture._video()]
    assert (
        module.push_digest(
            videos, sender=sender, seen=seen, cfg=cfg, summarizer=None, workdir=tmp_path
        )
        == 2
    )
    assert len(clients) == 1 and clients[0].is_closed
    assert options[0].get("trust_env", True) is (source != "bilibili")
    with factory() as injected:
        assert module.download_cover(
            "https://example.com/x", tmp_path / "injected.jpg", client=injected
        )
        assert not injected.is_closed
    clients.clear()
    module.push_digest(
        videos, sender=sender, seen=seen, cfg=cfg, summarizer=None, workdir=tmp_path, no_push=True
    )
    assert clients == []


def test_optional_cleanup_does_not_replace_business_exception():
    class BrokenClose:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise OSError("cleanup")

    with pytest.raises(ValueError, match="business"):
        with ExitStack() as stack:
            enter_optional_client(stack, BrokenClose())
            raise ValueError("business")


def test_ledger_cache_external_replace_and_interleaved_append(tmp_path, monkeypatch):
    sent_ledger.clear_cache()
    path = tmp_path / "ledger"

    def row(mid):
        return dict(chat_id=1, message_id=mid, url=f"https://x/{mid}", producer="youtube")

    sent_ledger.append_sent(**row(1), path=path)
    assert sent_ledger.lookup(1, 1, path=path)
    original_size = path.stat().st_size
    replacement = tmp_path / "replacement"
    replacement.write_text(path.read_text().replace("https://x/1", "https://x/9"))
    replacement.replace(path)
    assert path.stat().st_size == original_size
    assert sent_ledger.lookup(1, 1, path=path)["url"] == "https://x/9"
    with path.open("a") as handle:
        handle.write(json.dumps(row(2)) + "\n")
    sent_ledger.append_sent(**row(3), path=path)
    assert sent_ledger.lookup(1, 2, path=path)["url"] == "https://x/2"
    assert sent_ledger.lookup(1, 3, path=path)
    original = sent_ledger._load_index
    sent_ledger.clear_cache()
    reads = []
    monkeypatch.setattr(sent_ledger, "_load_index", lambda p: (reads.append(p), original(p))[1])
    assert sent_ledger.lookup(1, 1, path=path)
    assert sent_ledger.lookup(1, 2, path=path)
    assert len(reads) == 1


def test_ledger_failed_read_and_mid_read_change_do_not_poison_cache(tmp_path, monkeypatch):
    sent_ledger.clear_cache()
    path = tmp_path / "ledger"
    sent_ledger.append_sent(
        chat_id=1, message_id=1, url="https://x/1", producer="youtube", path=path
    )
    original = sent_ledger._load_index
    monkeypatch.setattr(
        sent_ledger, "_load_index", lambda p: (_ for _ in ()).throw(OSError("temporary"))
    )
    assert sent_ledger.lookup(1, 1, path=path) is None
    monkeypatch.setattr(sent_ledger, "_load_index", original)
    assert sent_ledger.lookup(1, 1, path=path)
    sent_ledger.clear_cache()

    def changed(p):
        loaded = original(p)
        with p.open("a") as handle:
            handle.write(json.dumps(dict(chat_id=1, message_id=2, url="https://x/2")) + "\n")
        return loaded

    monkeypatch.setattr(sent_ledger, "_load_index", changed)
    assert sent_ledger.lookup(1, 1, path=path)
    assert sent_ledger._index is None
    monkeypatch.setattr(sent_ledger, "_load_index", original)
    assert sent_ledger.lookup(1, 2, path=path)
