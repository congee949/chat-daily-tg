import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.evidence_index import (
    GeminiEmbeddingError,
    GeminiEmbedder,
    build_evidence_context_for_summary,
    build_evidence_index,
    cosine_similarity,
    extract_chunks,
    extract_claim_queries,
    render_evidence_hits,
)


class FakeEmbedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
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
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent",
        method="POST",
        json={"embedding": {"values": [0.1, 0.2]}},
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


def test_gemini_embedder_sanitizes_http_errors(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent",
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


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
