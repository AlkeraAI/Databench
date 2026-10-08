"""Reciprocal Rank Fusion, the corpus-agnostic hybrid core, shared by every hybrid search."""

from __future__ import annotations

import pytest
from alkera_cli.ranking import (
    fuse_to_ids,
    min_max_fusion,
    reciprocal_rank_fusion,
)


def test_rrf_rewards_agreement_across_rankings() -> None:
    # ``b`` is ranked highly by both lists → it should win.
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "a"]])
    assert [doc_id for doc_id, _score in fused] == ["b", "a", "c"]


def test_rrf_dedups_within_a_single_ranking() -> None:
    fused = reciprocal_rank_fusion([["a", "a", "b"]])
    ids = [doc_id for doc_id, _ in fused]
    assert ids == ["a", "b"]  # the duplicate ``a`` does not double-count


def test_rrf_weights_bias_a_ranking() -> None:
    # Two disjoint rankings: unweighted, the list-1 top wins the id tie-break;
    # weighting list 2 heavily lifts its top (``c``) above it.
    base = fuse_to_ids([["a", "x"], ["c", "y"]])
    weighted = fuse_to_ids([["a", "x"], ["c", "y"]], weights=[1.0, 10.0])
    assert base[0] == "a"
    assert weighted[0] == "c"


def test_rrf_weights_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="weights"):
        reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])


def test_fuse_to_ids_respects_limit() -> None:
    assert fuse_to_ids([["a", "b", "c", "d"]], limit=2) == ["a", "b"]


# --- score-aware min-max fusion (the tool-search hybrid) ---------------------


def test_min_max_fusion_is_query_adaptive() -> None:
    # Sparse is confident about ``a`` (high score), blind to ``z`` (absent);
    # dense is confident about ``z``. A weighted min-max sum keeps the confident
    # sparse hit on top while still surfacing the dense-only hit above noise.
    sparse = {"a": 10.0, "b": 1.0}
    dense = {"a": 0.2, "b": 0.1, "z": 0.9}
    fused = dict(min_max_fusion([sparse, dense], weights=[0.6, 0.4]))
    order = [doc_id for doc_id, _ in min_max_fusion([sparse, dense], weights=[0.6, 0.4])]
    assert order[0] == "a"  # confident sparse hit stays top
    assert "z" in fused and fused["z"] > 0  # dense-only hit still surfaces


def test_min_max_fusion_normalizes_disparate_scales() -> None:
    # Raw BM25 (~tens) and cosine (~0..1) live on different scales; min-max makes
    # each ranker's best = 1.0 so neither swamps the other.
    fused = dict(min_max_fusion([{"a": 50.0, "b": 10.0}, {"a": 0.3, "b": 0.6}], weights=[0.5, 0.5]))
    # ``a`` tops sparse (norm 1.0), ``b`` tops dense (norm 1.0) → they tie at 0.5.
    assert fused["a"] == pytest.approx(fused["b"])


def test_min_max_fusion_weights_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="weights"):
        min_max_fusion([{"a": 1.0}], weights=[1.0, 1.0])
