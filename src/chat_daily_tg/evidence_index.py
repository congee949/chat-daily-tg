from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Protocol

import httpx

_CLAIM_BULLET_RE = re.compile(r"^-\s+(?:\*\*(?P<title>[^*]+)\*\*[：:])?(?P<body>.+)$")


@dataclass(frozen=True)
class EvidenceChunk:
    source_id: str
    source_name: str
    time: str
    sender: str
    text: str


@dataclass(frozen=True)
class EvidenceHit:
    source_id: str
    source_name: str
    time: str
    sender: str
    text: str
    similarity: float


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        ...


_TELEGRAM_RE = re.compile(r"^\[Telegram / (?P<group>.+?) / (?P<time>\d{2}:\d{2}) / (?P<sender>.+?)\] (?P<text>.*)$")
_WX_HEADER_RE = re.compile(r"^### \d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2})$")
_WX_MESSAGE_RE = re.compile(r"^\*\*(?P<sender>[^*]+)\*\*:\s*(?P<text>.*)$")


def extract_chunks(groups_with_content: list[tuple[str, str]]) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    for source_index, (source_name, content) in enumerate(groups_with_content):
        chunks.extend(_extract_telegram_chunks(source_name, content, source_index=source_index))
        chunks.extend(_extract_wx_chunks(source_name, content, source_index=source_index))
    return chunks


def _extract_telegram_chunks(source_name: str, content: str, *, source_index: int) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    for idx, line in enumerate(content.splitlines()):
        match = _TELEGRAM_RE.match(line.strip())
        if not match:
            continue
        text = match.group("text").strip()
        if not text:
            continue
        group = match.group("group").strip()
        time = match.group("time").strip()
        sender = match.group("sender").strip()
        chunks.append(EvidenceChunk(
            source_id=f"{source_index}#{source_name}#{time}#{idx}",
            source_name=group or source_name,
            time=time,
            sender=sender,
            text=text,
        ))
    return chunks


def _extract_wx_chunks(source_name: str, content: str, *, source_index: int) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    current_time = ""
    for idx, line in enumerate(content.splitlines()):
        stripped = line.strip()
        header = _WX_HEADER_RE.match(stripped)
        if header:
            current_time = header.group("time")
            continue
        match = _WX_MESSAGE_RE.match(stripped)
        if not match:
            continue
        text = match.group("text").strip()
        if not text:
            continue
        chunks.append(EvidenceChunk(
            source_id=f"{source_index}#{source_name}#{current_time}#{idx}",
            source_name=source_name,
            time=current_time,
            sender=match.group("sender").strip(),
            text=text,
        ))
    return chunks


class GeminiEmbeddingError(RuntimeError):
    pass


class GeminiEmbedder:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout: float = 120.0,
        output_dimensionality: int | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.output_dimensionality = output_dimensionality

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, task_type="RETRIEVAL_DOCUMENT")

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, task_type="RETRIEVAL_QUERY")

    def _embed(self, texts: list[str], *, task_type: str) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for text in texts:
                body: dict[str, object] = {
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                }
                if self.output_dimensionality is not None:
                    body["outputDimensionality"] = self.output_dimensionality
                try:
                    response = client.post(
                        f"{self.endpoint}/models/{self.model}:embedContent",
                        headers={"x-goog-api-key": self.api_key},
                        json=body,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise GeminiEmbeddingError(
                        f"Gemini embedding request failed with HTTP {exc.response.status_code}"
                    ) from exc
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    raise GeminiEmbeddingError(f"Gemini embedding request failed: {type(exc).__name__}") from exc
                data = response.json()
                values = data.get("embedding", {}).get("values")
                if not isinstance(values, list):
                    raise GeminiEmbeddingError("Gemini embedding response missing embedding.values")
                vectors.append([float(v) for v in values])
        return vectors


class EvidenceIndex:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                source_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                time TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def replace(self, chunks: list[EvidenceChunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        with self.conn:
            self.conn.execute("DELETE FROM chunks")
            self.conn.executemany(
                """
                INSERT INTO chunks (source_id, source_name, time, sender, text, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.source_id,
                        chunk.source_name,
                        chunk.time,
                        chunk.sender,
                        chunk.text,
                        json.dumps(vector, separators=(",", ":")),
                    )
                    for chunk, vector in zip(chunks, vectors)
                ],
            )

    def search(self, query_vector: list[float], *, top_k: int, min_similarity: float = 0.0) -> list[EvidenceHit]:
        rows = self.conn.execute(
            "SELECT source_id, source_name, time, sender, text, embedding FROM chunks"
        ).fetchall()
        hits: list[EvidenceHit] = []
        for source_id, source_name, time, sender, text, raw_embedding in rows:
            similarity = cosine_similarity(query_vector, json.loads(raw_embedding))
            if similarity < min_similarity:
                continue
            hits.append(EvidenceHit(
                source_id=source_id,
                source_name=source_name,
                time=time,
                sender=sender,
                text=text,
                similarity=similarity,
            ))
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:top_k]


def build_evidence_index(
    *,
    index_path: Path,
    groups_with_content: list[tuple[str, str]],
    embedder: Embedder,
) -> EvidenceIndex:
    chunks = extract_chunks(groups_with_content)
    vectors = embedder.embed_documents([chunk.text for chunk in chunks])
    index = EvidenceIndex(index_path)
    try:
        index.replace(chunks, vectors)
    except Exception:
        index.close()
        raise
    return index


def extract_claim_queries(summary_text: str, *, limit: int = 12) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for line in summary_text.splitlines():
        match = _CLAIM_BULLET_RE.match(line.strip())
        if not match:
            continue
        title = (match.group("title") or "").strip()
        body = match.group("body").strip()
        query = f"{title} {body}".strip()
        query = re.sub(r"（[^）]+ / \d{2}:\d{2}[^）]*）$", "", query).strip()
        if not _is_high_risk_claim(query):
            continue
        if query in seen:
            continue
        seen.add(query)
        queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def build_evidence_context_for_summary(
    *,
    index: EvidenceIndex,
    embedder: Embedder,
    summary_text: str,
    top_k: int,
    min_similarity: float,
) -> str:
    queries = extract_claim_queries(summary_text)
    if not queries:
        return ""
    sections = []
    for query in queries:
        hits = retrieve_evidence_for_text(
            index=index,
            embedder=embedder,
            text=query,
            top_k=top_k,
            min_similarity=min_similarity,
        )
        sections.append(f"### Claim 查询：{query}\n{render_evidence_hits(hits)}")
    return "\n\n".join(sections)


def _is_high_risk_claim(text: str) -> bool:
    keywords = [
        "发布", "推出", "涨价", "降价", "封禁", "封锁", "退出", "裁员",
        "额度", "风控", "警告", "验证", "第一", "LiveBench", "版本",
        "Pro", "Plus", "Claude", "Grok", "GPT", "Codex", "Gemini",
        "美元", "元", "免税", "VPN", "政策", "红头文件",
    ]
    return any(keyword in text for keyword in keywords) or bool(re.search(r"\d+(?:\.\d+)+", text))


def retrieve_evidence_for_text(
    *,
    index: EvidenceIndex,
    embedder: Embedder,
    text: str,
    top_k: int,
    min_similarity: float,
) -> list[EvidenceHit]:
    vectors = embedder.embed_queries([text])
    if not vectors:
        return []
    return index.search(vectors[0], top_k=top_k, min_similarity=min_similarity)


def render_evidence_hits(hits: list[EvidenceHit]) -> str:
    if not hits:
        return "(未检索到高相似证据)"
    lines = []
    for hit in hits:
        source = f"{hit.source_name} / {hit.time}" if hit.time else hit.source_name
        sender = f" / {hit.sender}" if hit.sender else ""
        lines.append(f"- [{hit.similarity:.3f}] {source}{sender}: {hit.text}")
    return "\n".join(lines)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
