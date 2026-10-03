import json
import random
import sqlite3

import httpx

import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.evidence_index import (
    EmbeddingGeneration,
    EmbeddingGenerationMismatch,
    GENERATION_METADATA_COLUMNS,
    EvidenceChunk,
    EvidenceIndex,
    GeminiEmbeddingError,
    GeminiEmbedder,
    OpenAICompatibleEmbedder,
    OpenAICompatibleReranker,
    OpenAIEmbeddingError,
    RerankResult,
    RerankerError,
    build_evidence_context_for_summary,
    build_evidence_index,
    cosine_similarity,
    decode_vector,
    encode_vector,
    extract_chunks,
    extract_claim_queries,
    render_evidence_hits,
    retrieve_evidence_for_text,
)


TEST_GENERATION = EmbeddingGeneration(
    generation_id="evidence-test-v1",
    model_id="qwen-test",
    model_revision="weights-test",
    dimension=3,
    normalized=False,
    query_template="query-v1",
    document_template="document-v1",
)
TEST_GENERATION_2D = EmbeddingGeneration(
    generation_id="evidence-test-2d-v1",
    model_id="qwen-test",
    model_revision="weights-test",
    dimension=2,
    normalized=False,
)
OPENAI_TEST_GENERATION_2D = EmbeddingGeneration(
    generation_id="openai-evidence-test-2d-v1",
    model_id="qwen3-vl-embedding-8b-4bit",
    model_revision="weights-test",
    dimension=2,
    normalized=False,
)


@pytest.fixture(autouse=True)
def isolate_cross_process_inference_queue(tmp_path, monkeypatch):
    """HTTP mocks must not contend with the live runtime admission queue."""
    monkeypatch.setattr(
        "chat_daily_tg.inference_queue._queue_path",
        lambda _endpoint: tmp_path / "inference-queue.sqlite",
    )


class FakeEmbedder:
    def __init__(self):
        self.document_texts: list[str] = []
        self.query_batches: list[list[str]] = []
        self.generation = TEST_GENERATION

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_texts.extend(texts)
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_batches.append(list(texts))
        return self._embed(texts)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "4.3" in lowered or "claude" in lowered or "grok" in lowered or "实时语音" in lowered or "读x" in lowered:
                vectors.append([1.0, 0.0, 0.0])
            elif "vpn" in lowered or "红头文件" in lowered:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


def test_extract_chunks_reads_telegram_and_wechat_messages():
    chunks = extract_chunks([
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:22 / B] 这个能直接读x"),
        ("微信 / W1", "### 2026-05-06 10:00\n\n**Alice**: Claude 双倍额度活动"),
    ])

    assert any(chunk.source_name == "G1" and "4.3" in chunk.text for chunk in chunks)
    assert any(chunk.source_name == "微信 / W1" and "Claude" in chunk.text for chunk in chunks)


def test_extract_claim_queries_keeps_high_risk_bullets():
    queries = extract_claim_queries("""### 🧠 AI / 工具
- **Claude 4.3 发布**：实时语音第一（G1 / 14:15）
- **普通闲聊**：今天大家聊天很多（G1 / 12:00）
- **VPN 封堵传闻**：红头文件再起（G1 / 17:44）
""")

    assert any("Claude 4.3" in query for query in queries)
    assert any("VPN" in query for query in queries)
    assert not any("普通闲聊" in query for query in queries)


def test_build_evidence_index_and_retrieve_context(tmp_path):
    groups = [
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:22 / B] 这个能直接读x"),
        ("Telegram / G1", "[Telegram / G1 / 17:44 / C] 大陆封禁 vpn 是不是真的"),
    ]
    embedder = FakeEmbedder()
    index = build_evidence_index(index_path=tmp_path / "evidence.sqlite", groups_with_content=groups, embedder=embedder)

    context = build_evidence_context_for_summary(
        index=index,
        embedder=embedder,
        summary_text="- **Claude 4.3 发布**：实时语音第一（G1 / 14:15）",
        top_k=2,
        min_similarity=0.1,
    )

    assert "Claim 查询" in context
    assert "4.3出了哦" in context
    assert "能直接读x" in context
    index.close()


def test_evidence_context_reranks_dense_candidates_and_falls_back_on_error(tmp_path):
    index = EvidenceIndex(tmp_path / "rerank.sqlite", generation=TEST_GENERATION)
    chunks = [
        EvidenceChunk("a", "G", "10:00", "A", "Claude candidate A"),
        EvidenceChunk("b", "G", "10:01", "B", "Claude candidate B"),
        EvidenceChunk("c", "G", "10:02", "C", "Claude candidate C"),
    ]
    index.replace(chunks, [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.8, 0.2, 0.0]])
    embedder = FakeEmbedder()

    class ReverseReranker:
        def rerank(self, query, documents, *, top_n):
            assert len(documents) == 3
            return [RerankResult(1, 0.9), RerankResult(0, 0.8)]

    reranked = build_evidence_context_for_summary(
        index=index,
        embedder=embedder,
        summary_text="- **Claude 发布**：版本升级",
        top_k=2,
        min_similarity=0.0,
        reranker=ReverseReranker(),
        dense_top_k=3,
    )
    assert reranked.index("candidate B") < reranked.index("candidate A")

    class BrokenReranker:
        def rerank(self, *args, **kwargs):
            raise RuntimeError("reranker offline")

    fallback = build_evidence_context_for_summary(
        index=index,
        embedder=embedder,
        summary_text="- **Claude 发布**：版本升级",
        top_k=2,
        min_similarity=0.0,
        reranker=BrokenReranker(),
        dense_top_k=3,
    )
    assert fallback.index("candidate A") < fallback.index("candidate B")

    class PartialReranker:
        def rerank(self, *args, **kwargs):
            return [RerankResult(1, 0.9)]

    partial_fallback = build_evidence_context_for_summary(
        index=index,
        embedder=embedder,
        summary_text="- **Claude 发布**：版本升级",
        top_k=2,
        min_similarity=0.0,
        reranker=PartialReranker(),
        dense_top_k=3,
    )
    assert partial_fallback.index("candidate A") < partial_fallback.index("candidate B")
    index.close()


def test_evidence_context_skips_invalid_dense_generation_without_false_no_match(tmp_path):
    index = EvidenceIndex(tmp_path / "guard.sqlite", generation=TEST_GENERATION)
    chunk = EvidenceChunk("a", "G", "10:00", "A", "Claude candidate")
    index.replace([chunk], [[1.0, 0.0, 0.0]])
    with index.conn:
        index.conn.execute("UPDATE chunks SET generation_id='other-generation'")

    context = build_evidence_context_for_summary(
        index=index,
        embedder=FakeEmbedder(),
        summary_text="- **Claude 发布**：版本升级",
        top_k=1,
        min_similarity=0.0,
    )
    assert context == ""
    assert "未检索到" not in context
    index.close()


@pytest.mark.parametrize(
    "error",
    [
        OpenAIEmbeddingError("embedding runtime unavailable"),
        RuntimeError("inference queue unavailable"),
    ],
)
def test_query_time_evidence_enhancement_failures_return_empty_without_false_no_match(
    error,
):
    class FailingEmbedder:
        def embed_queries(self, _texts):
            raise error

    class UnusedIndex:
        def search(self, *_args, **_kwargs):
            raise AssertionError("search must not run after embedding failure")

    context = build_evidence_context_for_summary(
        index=UnusedIndex(),
        embedder=FailingEmbedder(),
        summary_text="- **Claude 发布**：版本升级",
        top_k=1,
        min_similarity=0.0,
    )
    hits = retrieve_evidence_for_text(
        index=UnusedIndex(),
        embedder=FailingEmbedder(),
        text="Claude 发布",
        top_k=1,
        min_similarity=0.0,
    )

    assert context == ""
    assert "未检索到" not in context
    assert hits == []


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_query_time_evidence_does_not_swallow_process_control_exceptions(error_type):
    class InterruptingEmbedder:
        def embed_queries(self, _texts):
            raise error_type()

    class UnusedIndex:
        pass

    with pytest.raises(error_type):
        build_evidence_context_for_summary(
            index=UnusedIndex(),
            embedder=InterruptingEmbedder(),
            summary_text="- **Claude 发布**：版本升级",
            top_k=1,
            min_similarity=0.0,
        )
    with pytest.raises(error_type):
        retrieve_evidence_for_text(
            index=UnusedIndex(),
            embedder=InterruptingEmbedder(),
            text="Claude 发布",
            top_k=1,
            min_similarity=0.0,
        )


def test_retrieve_evidence_sqlite_failure_is_query_time_fail_open(tmp_path):
    index = EvidenceIndex(tmp_path / "closed.sqlite", generation=TEST_GENERATION)
    index.replace(
        [EvidenceChunk("a", "G", "10:00", "A", "Claude candidate")],
        [[1.0, 0.0, 0.0]],
    )
    index.close()

    hits = retrieve_evidence_for_text(
        index=index,
        embedder=FakeEmbedder(),
        text="Claude 发布",
        top_k=1,
        min_similarity=0.0,
    )

    assert hits == []


def test_build_evidence_index_filters_low_information_idle_chat(tmp_path):
    groups = [
        ("Telegram / G1", "\n".join([
            "[Telegram / G1 / 10:00 / A] 哈哈哈",
            "[Telegram / G1 / 10:01 / B] ok",
            "[Telegram / G1 / 10:02 / C] 今天天气不错大家随便聊聊",
            "[Telegram / G1 / 10:03 / D] Claude 4.3 发布了，实时语音第一",
            "[Telegram / G1 / 10:04 / E] 这个能直接读x",
        ])),
    ]
    embedder = FakeEmbedder()

    index = build_evidence_index(index_path=tmp_path / "evidence.sqlite", groups_with_content=groups, embedder=embedder)

    assert "Claude 4.3 发布了，实时语音第一" in embedder.document_texts
    assert "这个能直接读x" in embedder.document_texts
    assert "哈哈哈" not in embedder.document_texts
    assert "ok" not in embedder.document_texts
    assert "今天天气不错大家随便聊聊" not in embedder.document_texts
    index.close()


def test_build_evidence_index_keeps_short_high_risk_messages(tmp_path):
    groups = [
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:16 / B] 哈哈"),
    ]
    embedder = FakeEmbedder()

    index = build_evidence_index(index_path=tmp_path / "evidence.sqlite", groups_with_content=groups, embedder=embedder)

    assert "4.3出了哦" in embedder.document_texts
    assert "哈哈" not in embedder.document_texts
    index.close()


def test_render_evidence_hits_empty():
    assert render_evidence_hits([]) == "(未检索到高相似证据)"


def test_extract_chunks_keeps_adjacent_short_messages_separate():
    chunks = extract_chunks([
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:16 / B] Claude 双倍额度活动"),
    ])

    assert len(chunks) == 2
    assert all("4.3出了哦\n" not in chunk.text for chunk in chunks)


def test_extract_chunks_source_ids_include_source_ordinal_for_duplicates():
    chunks = extract_chunks([
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] first"),
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] second"),
    ])

    assert len({chunk.source_id for chunk in chunks}) == 2


def test_gemini_embedder_sends_header_dimension_and_task_type(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        method="POST",
        json={"embeddings": [{"values": [0.1, 0.2]}]},
    )
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="secret-key",
        output_dimensionality=768,
    )

    vectors = embedder.embed_queries(["Claude 4.3"])

    assert vectors == [[0.1, 0.2]]
    request = httpx_mock.get_request()
    assert request.headers["x-goog-api-key"] == "secret-key"
    assert "secret-key" not in str(request.url)
    body = request.read().decode()
    assert '"taskType":"RETRIEVAL_QUERY"' in body.replace(" ", "")
    assert '"outputDimensionality":768' in body.replace(" ", "")
    assert '"model":"models/gemini-embedding-2"' in body.replace(" ", "")


def test_gemini_embedder_sanitizes_http_errors(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        method="POST",
        status_code=400,
        json={"error": {"message": "bad"}},
    )
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="secret-key",
    )

    with pytest.raises(GeminiEmbeddingError) as exc:
        embedder.embed_documents(["text"])

    assert "HTTP 400" in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_openai_embedder_batches_reorders_and_validates_dimension(httpx_mock: HTTPXMock):
    url = "http://127.0.0.1:8790/v1/embeddings"
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={"model": "qwen3-vl-embedding-8b-4bit", "revision": "weights-test", "data": [
            {"index": i, "embedding": [float(i), 1.0]}
            for i in reversed(range(32))
        ]},
    )
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={
            "model": "qwen3-vl-embedding-8b-4bit",
            "revision": "weights-test",
            "data": [{"index": 0, "embedding": [32.0, 1.0]}],
        },
    )
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen3-vl-embedding-8b-4bit",
        output_dimensionality=2,
        batch_size=32,
        generation=OPENAI_TEST_GENERATION_2D,
    )

    vectors = embedder.embed_documents([f"text {i}" for i in range(33)])

    assert vectors[0] == [0.0, 1.0]
    assert vectors[-1] == [32.0, 1.0]
    requests = httpx_mock.get_requests()
    assert [len(json.loads(request.read())["input"]) for request in requests] == [32, 1]
    assert json.loads(requests[0].read())["model"] == "qwen3-vl-embedding-8b-4bit"
    first_input = json.loads(requests[0].read())["input"][0]
    assert list(first_input) == ["text"]
    assert first_input["text"].endswith("[正文]\ntext 0")
    assert "Authorization" not in requests[0].headers


def test_openai_embedder_rejects_wrong_dimension(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8790/v1/embeddings",
        method="POST",
        json={
            "model": "qwen3-vl-embedding-8b-4bit",
            "revision": "weights-test",
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
        },
    )
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen3-vl-embedding-8b-4bit",
        output_dimensionality=4096,
        generation=EmbeddingGeneration(
            generation_id="qwen-4096-test",
            model_id="qwen3-vl-embedding-8b-4bit",
            model_revision="weights-test",
            dimension=4096,
            normalized=False,
        ),
    )

    with pytest.raises(OpenAIEmbeddingError, match="dimension 2, expected 4096"):
        embedder.embed_queries(["test"])


def test_openai_embedder_uses_distinct_stable_query_and_document_templates(
    httpx_mock: HTTPXMock,
):
    for _ in range(2):
        httpx_mock.add_response(
            url="http://127.0.0.1:8790/v1/embeddings",
            method="POST",
            json={
                "model": "qwen-test",
                "revision": "weights-test",
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
            },
        )
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen-test",
        output_dimensionality=2,
        generation=TEST_GENERATION_2D,
    )

    embedder.embed_documents(["same text"])
    embedder.embed_queries(["same text"])

    payloads = [json.loads(request.read())["input"][0]["text"]
                for request in httpx_mock.get_requests()]
    assert payloads == [
        "[文档用途] ChatDaily 语义检索候选\n[正文]\nsame text",
        "[任务] 检索与下列查询相关的 ChatDaily 内容\n[查询]\nsame text",
    ]


def test_openai_embedder_marks_queries_online_and_documents_offline(
    httpx_mock: HTTPXMock,
):
    for _ in range(2):
        httpx_mock.add_response(
            url="http://127.0.0.1:8790/v1/embeddings",
            method="POST",
            json={
                "model": "qwen-test",
                "revision": "weights-test",
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
            },
        )

    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    class RecordingQueue:
        def __init__(self):
            self.online_flags: list[bool] = []

        def acquire(self, *, online, deadline):
            self.online_flags.append(online)
            return Lease()

    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen-test",
        output_dimensionality=2,
        generation=TEST_GENERATION_2D,
    )
    queue = RecordingQueue()
    embedder._inference_queue = queue

    embedder.embed_documents(["offline document"])
    embedder.embed_queries(["online query"])

    assert queue.online_flags == [False, True]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}, "model mismatch"),
        ({"model": "wrong", "data": [{"index": 0, "embedding": [1.0, 0.0]}]},
         "model mismatch"),
        ({"model": "qwen-test", "revision": "weights-test",
          "data": [{"embedding": [1.0, 0.0]}]},
         "invalid index"),
        ({"model": "qwen-test", "revision": "weights-test", "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 0, "embedding": [0.0, 1.0]},
        ]}, "invalid index"),
        ({"model": "qwen-test", "revision": "weights-test", "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 2, "embedding": [0.0, 1.0]},
        ]}, "invalid index"),
    ],
)
def test_openai_embedder_rejects_unproven_response_mapping(payload, message):
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen-test",
        output_dimensionality=2,
        generation=TEST_GENERATION_2D,
    )
    expected = 2 if len(payload.get("data", [])) == 2 else 1
    with pytest.raises(OpenAIEmbeddingError, match=message):
        embedder._parse_response(payload, expected=expected)


@pytest.mark.parametrize(
    ("revision", "message"),
    [
        (None, "revision attestation is missing"),
        ("wrong-revision", "revision mismatch"),
    ],
)
def test_openai_embedder_requires_declared_response_revision(revision, message):
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://runtime-attestation.test/v1",
        model="qwen-test",
        output_dimensionality=2,
        generation=TEST_GENERATION_2D,
    )
    payload = {
        "model": "qwen-test",
        "data": [{"index": 0, "embedding": [1.0, 0.0]}],
    }
    if revision is not None:
        payload["revision"] = revision

    with pytest.raises(OpenAIEmbeddingError, match=message):
        embedder._parse_response(payload, expected=1)


def test_openai_embedder_keeps_generic_provider_compatibility_without_revision():
    generation = EmbeddingGeneration(
        generation_id="generic-openai-v1",
        model_id="generic-embedding",
        model_revision="unversioned",
        dimension=2,
        normalized=False,
    )
    embedder = OpenAICompatibleEmbedder(
        endpoint="https://generic-openai.test/v1",
        model="generic-embedding",
        output_dimensionality=2,
        generation=generation,
    )

    vectors = embedder._parse_response(
        {
            "model": "generic-embedding",
            "data": [{"index": 0, "embedding": [1.0, 0.0]}],
        },
        expected=1,
    )

    assert vectors == [[1.0, 0.0]]


def test_generic_embedding_config_without_revision_remains_compatible(monkeypatch):
    class Config:
        provider = "openai"
        endpoint = "https://generic-embedding.test/v1"
        model = "generic-embedding"
        model_revision = ""
        dimension = 2

    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("generic provider must not inspect bundled runtime config")
        ),
    )

    generation = EmbeddingGeneration.from_config(Config())

    assert generation.model_revision == "unversioned"


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8790/v1", "http://localhost:8790/v1"],
)
def test_loopback_embedding_config_rejects_undiscoverable_revision(
    monkeypatch, endpoint
):
    class Config:
        provider = "openai"
        model_revision = ""

    Config.endpoint = endpoint
    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda *_args: "",
    )

    with pytest.raises(ValueError, match="embedding revision fingerprint is unavailable"):
        EmbeddingGeneration.from_config(Config())


def test_openai_embedder_retry_and_backoff_never_cross_absolute_deadline(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class TimingOutClient:
        def __init__(self, clock):
            self.clock = clock
            self.timeouts = []

        def post(self, *args, timeout, **kwargs):
            self.timeouts.append(timeout)
            # Consume request time without ever returning a response.
            self.clock.now += 0.1 if len(self.timeouts) == 1 else timeout
            raise httpx.TimeoutException("simulated timeout")

    clock = Clock()
    client = TimingOutClient(clock)
    monkeypatch.setattr("chat_daily_tg.evidence_index.time.monotonic", clock.monotonic)
    monkeypatch.setattr("chat_daily_tg.evidence_index.time.sleep", clock.sleep)
    monkeypatch.setattr(random, "uniform", lambda *_args: 0.0)
    embedder = OpenAICompatibleEmbedder(
        endpoint="http://127.0.0.1:8790/v1",
        model="qwen-test",
        timeout=10.0,
        output_dimensionality=2,
        generation=TEST_GENERATION_2D,
    )

    with pytest.raises(OpenAIEmbeddingError, match="deadline exhausted"):
        embedder._embed_batch(client, ["payload"], deadline=1.0)

    assert len(client.timeouts) == 2
    assert client.timeouts == pytest.approx([1.0, 0.4])
    assert clock.now == pytest.approx(1.0)


def test_openai_reranker_posts_bounded_candidates_and_preserves_index_mapping(
    httpx_mock: HTTPXMock,
):
    httpx_mock.add_response(
        url="http://reranker-contract.test/v1/rerank",
        method="POST",
        json={
            "model": "qwen3-vl-reranker-2b-4bit",
            "results": [
                {"index": 1, "relevance_score": 0.91},
                {"index": 0, "relevance_score": 0.72},
            ]
        },
    )
    reranker = OpenAICompatibleReranker(
        endpoint="http://reranker-contract.test/v1",
        model="qwen3-vl-reranker-2b-4bit",
    )

    results = reranker.rerank("query", ["doc a", "doc b"], top_n=2)

    assert [result.index for result in results] == [1, 0]
    request = httpx_mock.get_request()
    assert json.loads(request.read()) == {
        "model": "qwen3-vl-reranker-2b-4bit",
        "query": "query",
        "documents": ["doc a", "doc b"],
        "top_n": 2,
    }


def test_openai_reranker_rejects_duplicate_index(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://reranker-contract.test/v1/rerank",
        method="POST",
        json={
            "model": "qwen3-vl-reranker-2b-4bit",
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.8},
            ]
        },
    )
    reranker = OpenAICompatibleReranker(
        endpoint="http://reranker-contract.test/v1",
        model="qwen3-vl-reranker-2b-4bit",
    )
    with pytest.raises(RerankerError, match="invalid index"):
        reranker.rerank("query", ["doc a", "doc b"], top_n=2)


def test_openai_reranker_rejects_wrong_response_model(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://reranker-contract.test/v1/rerank",
        method="POST",
        json={
            "model": "wrong-model",
            "results": [{"index": 0, "relevance_score": 0.9}],
        },
    )
    reranker = OpenAICompatibleReranker(
        endpoint="http://reranker-contract.test/v1",
        model="qwen3-vl-reranker-2b-4bit",
    )
    with pytest.raises(RerankerError, match="model mismatch"):
        reranker.rerank("query", ["doc"], top_n=1)


@pytest.mark.parametrize(
    ("revision", "message"),
    [
        (None, "revision attestation is missing"),
        ("wrong-revision", "revision mismatch"),
    ],
)
def test_openai_reranker_requires_declared_response_revision(
    httpx_mock: HTTPXMock, revision, message
):
    url = f"http://reranker-attestation-{revision or 'missing'}.test/v1/rerank"
    payload = {
        "model": "qwen-reranker",
        "results": [{"index": 0, "relevance_score": 0.9}],
    }
    if revision is not None:
        payload["revision"] = revision
    httpx_mock.add_response(url=url, method="POST", json=payload)
    reranker = OpenAICompatibleReranker(
        endpoint=url.removesuffix("/rerank"),
        model="qwen-reranker",
        expected_revision="reranker-weights-test",
    )

    with pytest.raises(RerankerError, match=message):
        reranker.rerank("query", ["doc"], top_n=1)


def test_openai_reranker_accepts_matching_declared_response_revision(
    httpx_mock: HTTPXMock,
):
    httpx_mock.add_response(
        url="http://reranker-attestation-match.test/v1/rerank",
        method="POST",
        json={
            "model": "qwen-reranker",
            "revision": "reranker-weights-test",
            "results": [{"index": 0, "relevance_score": 0.9}],
        },
    )
    reranker = OpenAICompatibleReranker(
        endpoint="http://reranker-attestation-match.test/v1",
        model="qwen-reranker",
        expected_revision="reranker-weights-test",
    )

    results = reranker.rerank("query", ["doc"], top_n=1)

    assert results == [RerankResult(index=0, relevance_score=0.9)]


def test_reranker_config_prefers_declared_revision_and_discovers_loopback_fingerprint(
    monkeypatch,
):
    class Config:
        endpoint = "http://127.0.0.1:8790/v1"
        model = "qwen-reranker"
        api_key_env = ""
        timeout = 8.0
        model_revision = ""
        reranker_revision = ""

    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda endpoint, key: "discovered-reranker-fingerprint",
    )
    discovered = OpenAICompatibleReranker.from_config(Config())
    assert discovered.expected_revision == "discovered-reranker-fingerprint"

    Config.model_revision = "declared-reranker-fingerprint"
    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("explicit revision must skip filesystem discovery")
        ),
    )
    declared = OpenAICompatibleReranker.from_config(Config())
    assert declared.expected_revision == "declared-reranker-fingerprint"


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8790/v1", "http://localhost:8790/v1"],
)
def test_loopback_reranker_config_rejects_undiscoverable_revision(
    monkeypatch, endpoint
):
    class Config:
        model = "qwen-reranker"
        api_key_env = ""
        timeout = 8.0
        model_revision = ""
        reranker_revision = ""

    Config.endpoint = endpoint
    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda *_args: "",
    )

    with pytest.raises(RerankerError, match="reranker revision fingerprint is unavailable"):
        OpenAICompatibleReranker.from_config(Config())


def test_generic_reranker_config_without_revision_remains_compatible(monkeypatch):
    class Config:
        endpoint = "https://generic-reranker.test/v1"
        model = "generic-reranker"
        api_key_env = ""
        timeout = 8.0
        model_revision = ""

    monkeypatch.setattr(
        "chat_daily_tg.evidence_index._discover_loopback_runtime_revision",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("generic provider must not inspect bundled runtime config")
        ),
    )

    reranker = OpenAICompatibleReranker.from_config(Config())

    assert reranker.expected_revision == ""


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0


def test_gemini_embedder_embeds_up_to_100_texts_in_single_request_without_sleep(
    httpx_mock: HTTPXMock, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr("chat_daily_tg.evidence_index.time.sleep", sleeps.append)
    httpx_mock.add_response(
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        method="POST",
        json={"embeddings": [{"values": [0.1, 0.2]} for _ in range(56)]},
    )
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="key",
    )

    vectors = embedder.embed_documents([f"text {i}" for i in range(56)])

    assert len(vectors) == 56
    assert len(httpx_mock.get_requests()) == 1
    assert sleeps == []


def test_gemini_embedder_splits_over_100_texts_and_sleeps_only_between_batches(
    httpx_mock: HTTPXMock, monkeypatch
):
    import json as _json

    sleeps: list[float] = []
    monkeypatch.setattr("chat_daily_tg.evidence_index.time.sleep", sleeps.append)
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    httpx_mock.add_response(
        url=url, method="POST",
        json={"embeddings": [{"values": [0.1]} for _ in range(100)]},
    )
    httpx_mock.add_response(
        url=url, method="POST",
        json={"embeddings": [{"values": [0.2]} for _ in range(30)]},
    )
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="key",
    )

    vectors = embedder.embed_documents([f"text {i}" for i in range(130)])

    assert len(vectors) == 130
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert len(_json.loads(requests[0].read())["requests"]) == 100
    assert len(_json.loads(requests[1].read())["requests"]) == 30
    assert sleeps == [GeminiEmbedder._INTER_BATCH_DELAY]


def test_build_evidence_context_embeds_all_claim_queries_in_one_call(tmp_path):
    groups = [
        ("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:22 / B] 这个能直接读x"),
        ("Telegram / G1", "[Telegram / G1 / 17:44 / C] 大陆封禁 vpn 是不是真的"),
    ]
    embedder = FakeEmbedder()
    index = build_evidence_index(index_path=tmp_path / "evidence.sqlite", groups_with_content=groups, embedder=embedder)

    context = build_evidence_context_for_summary(
        index=index,
        embedder=embedder,
        summary_text=(
            "- **Claude 4.3 发布**：实时语音第一（G1 / 14:15）\n"
            "- **VPN 封堵传闻**：红头文件再起（G1 / 17:44）\n"
        ),
        top_k=2,
        min_similarity=0.1,
    )

    assert len(embedder.query_batches) == 1
    assert len(embedder.query_batches[0]) == 2
    assert "4.3出了哦" in context
    assert "大陆封禁 vpn 是不是真的" in context
    index.close()


def test_gemini_embedder_retries_on_429_then_succeeds(httpx_mock: HTTPXMock):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    httpx_mock.add_response(url=url, method="POST", status_code=429, json={"error": {"message": "rate limit"}})
    httpx_mock.add_response(url=url, method="POST", status_code=429, json={"error": {"message": "rate limit"}})
    httpx_mock.add_response(url=url, method="POST", json={"embeddings": [{"values": [0.5, 0.6]}]})
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="key",
    )
    embedder._BASE_DELAY = 0.01  # speed up test
    vectors = embedder.embed_queries(["test"])
    assert vectors == [[0.5, 0.6]]
    assert len(httpx_mock.get_requests()) == 3


def test_gemini_embedder_raises_after_max_retries_on_429(httpx_mock: HTTPXMock):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    for _ in range(10):
        httpx_mock.add_response(url=url, method="POST", status_code=429, json={"error": {"message": "rate limit"}})
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="key",
    )
    embedder._BASE_DELAY = 0.01
    embedder._MAX_DELAY = 0.02
    with pytest.raises(GeminiEmbeddingError, match="failed after"):
        embedder.embed_documents(["text"])


def test_gemini_embedder_inter_batch_delay_class_attributes():
    assert GeminiEmbedder._INTER_BATCH_DELAY == 2.0
    assert GeminiEmbedder._THROTTLED_INTER_BATCH_DELAY == 16.0


def test_gemini_embedder_429_escalates_later_inter_batch_delays(
    httpx_mock: HTTPXMock, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr("chat_daily_tg.evidence_index.time.sleep", sleeps.append)
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    httpx_mock.add_response(
        url=url, method="POST",
        json={"embeddings": [{"values": [0.1]} for _ in range(100)]},
    )
    httpx_mock.add_response(
        url=url, method="POST", status_code=429, json={"error": {"message": "rate limit"}},
    )
    httpx_mock.add_response(
        url=url, method="POST",
        json={"embeddings": [{"values": [0.2]} for _ in range(100)]},
    )
    httpx_mock.add_response(
        url=url, method="POST",
        json={"embeddings": [{"values": [0.3]} for _ in range(50)]},
    )
    embedder = GeminiEmbedder(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-embedding-2",
        api_key="key",
    )
    embedder._BASE_DELAY = 0.01  # retry sleep stays < 2.0 and cannot collide below

    vectors = embedder.embed_documents([f"text {i}" for i in range(250)])

    assert len(vectors) == 250
    inter_batch = [s for s in sleeps if s in (2.0, 16.0)]
    assert inter_batch == [
        GeminiEmbedder._INTER_BATCH_DELAY,
        GeminiEmbedder._THROTTLED_INTER_BATCH_DELAY,
    ]


def test_encode_decode_vector_blob_legacy_json_and_garbage():
    vec = [0.25, -1.5, 3.0]  # exactly representable in float32
    raw = encode_vector(vec)
    assert isinstance(raw, bytes) and len(raw) == 12
    assert decode_vector(raw) == vec
    assert decode_vector(memoryview(raw)) == vec
    assert decode_vector(json.dumps(vec)) == vec
    assert decode_vector(encode_vector([0.1])) == pytest.approx([0.1], rel=1e-6)
    assert decode_vector(None) is None
    assert decode_vector("not json") is None
    assert decode_vector('{"a": 1}') is None
    assert decode_vector(b"\x00\x01\x02") is None  # not a multiple of 4 bytes
    assert decode_vector(123) is None


def test_evidence_index_writes_blob_and_disables_mixed_legacy_generation(tmp_path):
    index = EvidenceIndex(tmp_path / "evidence.sqlite", generation=TEST_GENERATION_2D)
    chunk = EvidenceChunk(source_id="0#G1#14:15#0", source_name="G1",
                          time="14:15", sender="A", text="Claude 4.3 发布")
    index.replace([chunk], [[1.0, 0.0]])
    raw = index.conn.execute("SELECT embedding FROM chunks").fetchone()[0]
    assert isinstance(raw, bytes)

    index.conn.execute(
        "INSERT INTO chunks(source_id,source_name,time,sender,text,embedding) "
        "VALUES (?,?,?,?,?,?)",
        ("1#G1#15:00#0", "G1", "15:00", "B", "旧格式行", json.dumps([1.0, 0.0])),
    )
    index.conn.commit()

    with pytest.raises(EmbeddingGenerationMismatch, match="incomplete/incompatible"):
        index.search([1.0, 0.0], top_k=5, min_similarity=0.5)
    index.close()


def test_evidence_index_legacy_migration_leaves_all_generation_columns_null(tmp_path):
    path = tmp_path / "legacy-evidence.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chunks (source_id TEXT PRIMARY KEY, source_name TEXT NOT NULL, "
        "time TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, "
        "embedding TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
        ("legacy", "G", "10:00", "A", "old", json.dumps([1.0, 0.0])),
    )
    conn.commit()
    conn.close()

    index = EvidenceIndex(path, generation=TEST_GENERATION_2D)
    names = [name for name, _ in GENERATION_METADATA_COLUMNS]
    row = index.conn.execute(
        "SELECT " + ",".join(names) + " FROM chunks WHERE source_id='legacy'"
    ).fetchone()
    assert tuple(row) == (None,) * len(names)
    with pytest.raises(EmbeddingGenerationMismatch):
        index.search([1.0, 0.0], top_k=1)
    index.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("model_revision", "other-weights"),
        ("query_template", "query-v2"),
        ("document_template", "document-v2"),
        ("payload_version", "text-v2"),
        ("normalization_version", "chatdaily-normalization-v2"),
        ("chunker_version", "chatdaily-chunker-v2"),
        ("context_hash", "0" * 64),
    ],
)
def test_evidence_generation_id_cannot_bypass_full_context_guard(
    tmp_path, column, value
):
    index = EvidenceIndex(tmp_path / f"guard-{column}.sqlite", generation=TEST_GENERATION_2D)
    chunk = EvidenceChunk("a", "G", "10:00", "A", "Claude candidate")
    index.replace([chunk], [[1.0, 0.0]])
    # generation_id/model_id/dimension remain unchanged; one full-context
    # mismatch must still disable dense retrieval.
    with index.conn:
        index.conn.execute(f"UPDATE chunks SET {column}=?", (value,))
    with pytest.raises(EmbeddingGenerationMismatch, match="incomplete/incompatible"):
        index.search([1.0, 0.0], top_k=1)
    index.close()


def test_embedding_context_identity_includes_normalization_and_chunker_versions():
    identity = TEST_GENERATION.context_identity()

    assert identity["normalization_version"] == "chatdaily-normalization-v1"
    assert identity["chunker_version"] == "chatdaily-chunker-v1"


def test_search_decodes_rows_once_and_replace_invalidates_cache(tmp_path, monkeypatch):
    import chat_daily_tg.evidence_index as ei

    calls: list[object] = []
    real = decode_vector
    monkeypatch.setattr(ei, "decode_vector", lambda raw: calls.append(raw) or real(raw))

    index = EvidenceIndex(tmp_path / "evidence.sqlite", generation=TEST_GENERATION_2D)
    chunks = [
        EvidenceChunk(source_id=f"s{i}", source_name="G", time="10:00",
                      sender="A", text=f"内容 {i}")
        for i in range(3)
    ]
    index.replace(chunks, [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])

    first = index.search([1.0, 0.0], top_k=3)
    second = index.search([1.0, 0.0], top_k=3)
    assert len(calls) == 3  # decoded once per row, not once per search
    assert [h.source_id for h in first] == [h.source_id for h in second]
    assert [h.similarity for h in first] == [h.similarity for h in second]

    index.replace(chunks[:1], [[1.0, 0.0]])
    assert len(index.search([1.0, 0.0], top_k=3)) == 1  # cache refreshed
    assert len(calls) == 4
    index.close()
