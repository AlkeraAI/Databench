"""A tiny in-process BM25 index — zero dependency, deterministic.

Backs ``ToolRegistry.search`` (the daemon-side ``search_tools`` meta-tool).
The corpus is the project's tool catalog (hundreds of docs at most), so a
hand-rolled BM25 is cheaper than a dependency and gives us a stable, testable
ranking. Rebuilt only when the catalog changes, never per query.

Tokenization splits on non-alphanumerics AND on ``snake_case`` / dotted /
camelCase boundaries, so ``sql.query`` / ``unload_to_stage`` / ``runSQL`` all
tokenize into their parts — matching how a model phrases a search.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_K1 = 1.5
_B = 0.75

# Split on non-alphanumerics, snake_case, camelCase humps, and ACRONYM→Word
# boundaries ("SQLNow" → "SQL" + "Now").
_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _SPLIT.split(text) if t]


@dataclass(frozen=True)
class _Doc:
    doc_id: str
    tokens: tuple[str, ...]
    length: int
    counts: dict[str, int]


class Bm25Index:
    """An immutable BM25 index over ``{doc_id: text}``."""

    def __init__(self, documents: dict[str, str]) -> None:
        self._docs: list[_Doc] = []
        df: Counter[str] = Counter()
        for doc_id, text in documents.items():
            tokens = tuple(tokenize(text))
            counts = dict(Counter(tokens))
            self._docs.append(_Doc(doc_id, tokens, len(tokens), counts))
            for term in counts:
                df[term] += 1
        n = len(self._docs)
        self._avgdl = (sum(d.length for d in self._docs) / n) if n else 0.0
        # BM25 idf with the +1 smoothing so it's never negative.
        self._idf: dict[str, float] = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }

    def search(self, query: str, *, k: int = 8) -> list[tuple[str, float]]:
        """Return up to ``k`` ``(doc_id, score)`` pairs, best first. Ties broken
        by ``doc_id`` so the ranking is deterministic."""
        q_terms = tokenize(query)
        if not q_terms or not self._docs:
            return []
        scored: list[tuple[str, float]] = []
        for doc in self._docs:
            score = 0.0
            for term in q_terms:
                tf = doc.counts.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + _K1 * (1 - _B + _B * (doc.length / self._avgdl if self._avgdl else 0))
                score += idf * (tf * (_K1 + 1)) / denom
            if score > 0:
                scored.append((doc.doc_id, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:k]


__all__ = ["Bm25Index", "tokenize"]
