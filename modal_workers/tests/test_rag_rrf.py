"""Tests for RRF (Reciprocal Rank Fusion) — pure math, no I/O.

Run: python -m pytest modal_workers/tests/test_rag_rrf.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

from modal_workers.rag.hybrid_search import (
    RRF_K, _cache_key, _normalize_query, reciprocal_rank_fusion,
)


def test_rrf_empty_legs_returns_empty():
    assert reciprocal_rank_fusion([], []) == []


def test_rrf_dense_only_ranks_by_dense_position():
    # Dense returns 3 chunks; BM25 returns nothing
    dense = [("c1", 0.9), ("c2", 0.8), ("c3", 0.7)]
    fused = reciprocal_rank_fusion([], dense)
    assert [f[0] for f in fused] == ["c1", "c2", "c3"]
    # Top score = 1 / (k + 1)
    assert abs(fused[0][1] - 1 / (RRF_K + 1)) < 1e-9
    # Each entry has dense_rank set, bm25_rank None
    for cid, score, b, d in fused:
        assert b is None
        assert d is not None


def test_rrf_bm25_only():
    bm25 = [("c1", 0.5), ("c2", 0.3)]
    fused = reciprocal_rank_fusion(bm25, [])
    assert [f[0] for f in fused] == ["c1", "c2"]
    for cid, score, b, d in fused:
        assert b is not None
        assert d is None


def test_rrf_intersection_promoted():
    # Both legs rank c1 first; intersection should beat singleton
    bm25 = [("c1", 0.9), ("c2", 0.5)]
    dense = [("c1", 0.95), ("c3", 0.7)]
    fused = reciprocal_rank_fusion(bm25, dense)
    by_id = {f[0]: f[1] for f in fused}
    assert by_id["c1"] > by_id["c2"]
    assert by_id["c1"] > by_id["c3"]


def test_rrf_returns_all_unique_ids():
    bm25 = [("a", 0.9), ("b", 0.5), ("c", 0.1)]
    dense = [("d", 0.9), ("a", 0.5), ("e", 0.1)]
    fused = reciprocal_rank_fusion(bm25, dense)
    assert {f[0] for f in fused} == {"a", "b", "c", "d", "e"}


def test_rrf_intersection_score_equals_sum():
    # c1 at rank 1 in both legs → score = 2 / (k + 1)
    bm25 = [("c1", 1.0)]
    dense = [("c1", 1.0)]
    fused = reciprocal_rank_fusion(bm25, dense)
    assert len(fused) == 1
    assert abs(fused[0][1] - 2 / (RRF_K + 1)) < 1e-9


def test_rrf_lower_rank_penalty():
    # Same chunk at rank 1 vs rank 50 — score drops noticeably
    bm25_high = [("c1", 0.9)] + [(f"x{i}", 0.1) for i in range(0)]
    bm25_low = [(f"x{i}", 0.9) for i in range(50)] + [("c1", 0.1)]
    high = reciprocal_rank_fusion(bm25_high, [])
    low = reciprocal_rank_fusion(bm25_low, [])
    high_score = [f[1] for f in high if f[0] == "c1"][0]
    low_score = [f[1] for f in low if f[0] == "c1"][0]
    assert high_score > low_score


# ---------------------------------------------------------------------------
# Query normalization & cache key determinism
# ---------------------------------------------------------------------------

def test_normalize_query_lowercases_and_collapses_whitespace():
    assert _normalize_query("AXSM   CGRP\n\nphase 3") == "axsm cgrp phase 3"


def test_cache_key_deterministic_across_runs():
    k1 = _cache_key("q", {"corpus": "literature"}, 8, "rerank-2.5")
    k2 = _cache_key("q", {"corpus": "literature"}, 8, "rerank-2.5")
    assert k1 == k2


def test_cache_key_changes_on_input_change():
    base = _cache_key("q", {"corpus": "literature"}, 8, "rerank-2.5")
    assert base != _cache_key("q2", {"corpus": "literature"}, 8, "rerank-2.5")
    assert base != _cache_key("q", {"corpus": "filings"}, 8, "rerank-2.5")
    assert base != _cache_key("q", {"corpus": "literature"}, 16, "rerank-2.5")
    assert base != _cache_key("q", {"corpus": "literature"}, 8, "rerank-3.5")


def test_cache_key_normalizes_query_case():
    k1 = _cache_key("AXSM   CGRP", {}, 8, "x")
    k2 = _cache_key("axsm cgrp", {}, 8, "x")
    assert k1 == k2
