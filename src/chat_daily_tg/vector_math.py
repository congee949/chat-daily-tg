"""Exact cosine arithmetic shared by evidence retrieval and topic dedup."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


if hasattr(math, "sumprod"):
    _sumprod = math.sumprod
else:

    def _sumprod(left: Sequence[float], right: Sequence[float]) -> float:
        return sum(a * b for a, b in zip(left, right))


def vector_norm(vector: Sequence[float]) -> float:
    return math.sqrt(_sumprod(vector, vector))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = _sumprod(left, right)
    left_norm, right_norm = vector_norm(left), vector_norm(right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class CosineScorer:
    """Snapshot of validated vectors with reusable norms, in input order.

    No normalization or approximate arithmetic: thresholds use exactly the
    same dot / (left_norm * right_norm) as cosine_similarity. Copies prevent
    caller mutation from making a cached norm disagree with its vector.
    """

    def __init__(self, vectors: Iterable[Sequence[float] | None] = ()):
        self._vectors: list[tuple[tuple[float, ...], float]] = []
        for vector in vectors:
            self.append(vector)

    def append(self, vector: Sequence[float] | None) -> None:
        snapshot = tuple(vector) if vector is not None else ()
        self._vectors.append((snapshot, vector_norm(snapshot)))

    def similarities(self, query: Sequence[float]) -> list[float]:
        query_norm = vector_norm(query)
        if not query or query_norm == 0.0:
            return [0.0] * len(self._vectors)
        return [
            _sumprod(query, vector) / (query_norm * norm)
            if len(query) == len(vector) and norm != 0.0
            else 0.0
            for vector, norm in self._vectors
        ]
