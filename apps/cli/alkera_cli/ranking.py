"""Corpus-agnostic rank fusion for hybrid retrieval.

``reciprocal_rank_fusion`` fuses any number of ranked id-lists (e.g. a BM25
ranking and a dense-vector ranking) into one ordering by Reciprocal Rank Fusion
(RRF): each list contributes ``1 / (k + rank)`` per id. It operates on plain
ids — NOT on KB rows — so the tool-search hybrid ranker reuses it over a
tool-spec corpus with zero coupling to the context schema.

RRF is the high-ROI hybrid core: no score normalization,
robust to the very different score scales of sparse vs dense rankers. A
cross-encoder rerank is a later, optional top-N pass layered on top.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

DEFAULT_RRF_K = 60
"""The standard RRF damping constant — larger ⇒ flatter rank weighting."""


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id-lists into one ``(id, score)`` ordering, best first.

    ``rankings`` is a list of rankings, each a list of ids in best-first order
    (duplicates within a list are ignored after first occurrence). ``weights``,
    if given, scales each ranking's contribution (defaults to all-1.0). Ties
    break by id for determinism.
    """
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("weights must match the number of rankings")
    scores: dict[str, float] = {}
    for ri, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else weights[ri]
        seen: set[str] = set()
        for rank, doc_id in enumerate(ranking):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def fuse_to_ids(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
    weights: Sequence[float] | None = None,
) -> list[str]:
    """``reciprocal_rank_fusion`` projected to just the fused ids (capped)."""
    fused = reciprocal_rank_fusion(rankings, k=k, weights=weights)
    ids = [doc_id for doc_id, _score in fused]
    return ids if limit is None else ids[:limit]


def _min_max(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a score map to ``[0, 1]``.

    A FLAT arm (all scores equal → no discriminating signal) normalizes to all
    ``0.0``, not all ``1.0``: a no-signal ranker must stay NEUTRAL in the weighted
    sum, never inject a full-weight bonus that would suppress the other arm's
    confident hits (e.g. an all-tied BM25 arm burying a pure-semantic dense hit)."""
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi <= lo:
        return dict.fromkeys(scores, 0.0)
    span = hi - lo
    return {doc_id: (value - lo) / span for doc_id, value in scores.items()}


def min_max_fusion(
    sources: Sequence[dict[str, float]],
    *,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Score-aware hybrid fusion: min-max normalize each source's scores to a
    common ``[0, 1]`` scale, then take a weighted sum over the union of ids
    (best first, ties by id).

    Unlike rank-only RRF, this keeps each ranker's *score margin* and is
    QUERY-ADAPTIVE: when one ranker has no real signal for a query its (still
    normalized) contribution is small relative to the confident ranker's peak, so
    a confident BM25 hit isn't displaced — yet a strong dense-only hit (the
    pure-semantic paraphrase BM25 can't get) still surfaces. Corpus-agnostic: it
    operates on plain ``{id: score}`` maps, so it backs the tool-search hybrid
    over a tool-spec corpus exactly as over the KB.
    """
    if weights is not None and len(weights) != len(sources):
        raise ValueError("weights must match the number of sources")
    fused: dict[str, float] = {}
    for index, source in enumerate(sources):
        weight = 1.0 if weights is None else weights[index]
        for doc_id, value in _min_max(source).items():
            fused[doc_id] = fused.get(doc_id, 0.0) + weight * value
    return sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))


DENSE_SIMILARITY_FLOOR = 0.58
"""The cosine similarity a vector-only hit needs to be a candidate at all. A vector
search always answers with nearest neighbours, however far they are, so without a
floor a query about nothing ("zzqx purple elephants") returned the k items least
unlike it. A keyword hit (a query word in the item) is never cut by it.

Measured with the shipped bge-small model on the retrieval evals: nonsense queries
reach at most 0.55 against the KB and the tool catalog, and the weakest relevant
pure-semantic pair (the zero-overlap litmus cases) sits at 0.60.
``ALKERA_CONTEXT_DENSE_FLOOR`` overrides it for another model."""
ENV_DENSE_FLOOR = "ALKERA_CONTEXT_DENSE_FLOOR"


def dense_similarity_floor() -> float:
    """The floor in force, read at call time so an override is honoured."""
    raw = os.environ.get(ENV_DENSE_FLOOR, "").strip()
    try:
        return float(raw) if raw else DENSE_SIMILARITY_FLOOR
    except ValueError:
        return DENSE_SIMILARITY_FLOOR


__all__ = [
    "DEFAULT_RRF_K",
    "DENSE_SIMILARITY_FLOOR",
    "ENV_DENSE_FLOOR",
    "dense_similarity_floor",
    "fuse_to_ids",
    "min_max_fusion",
    "reciprocal_rank_fusion",
]
