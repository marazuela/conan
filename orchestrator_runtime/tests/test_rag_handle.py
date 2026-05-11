"""Tests for `orchestrator_runtime.rag_handle` — pure shape + asset_id
expansion via a stubbed Supabase client. No network.

Run: python -m pytest orchestrator_runtime/tests/test_rag_handle.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


class FakeSb:
    """Minimal `_rest`-shaped stub. Records calls + returns canned responses."""

    def __init__(self, canned: Dict[tuple, Any]):
        self.canned = canned
        self.calls: List[tuple] = []

    def _rest(self, method: str, path: str, *,
              params: Optional[Dict[str, Any]] = None,
              json_body: Optional[Dict[str, Any]] = None,
              prefer: Optional[str] = None) -> Any:
        self.calls.append((method, path, params, json_body))
        # Find first canned key prefix-matching (method, path).
        for (m, p), val in self.canned.items():
            if m == method and p == path:
                return val
        return []


def _fake_chunkhit(chunk_id: str, document_id: str, text: str = "x"):
    from modal_workers.rag.hybrid_search import ChunkHit
    return ChunkHit(
        chunk_id=chunk_id, document_id=document_id, chunk_text=text,
        contextual_prefix="prefix", section_path=["sec"],
        score=0.5, rerank_score=0.7, source="pubmed",
        title="t", published_at="2026-01-01",
    )


def test_hybrid_search_returns_dict_shape(monkeypatch):
    """Result rows include all the expected keys with correct types."""
    from orchestrator_runtime import rag_handle

    sb = FakeSb({})

    def stub_hs(_sb, _query, _corpus, *, k=8,
                document_ids_filter=None, rerank=True):
        return [_fake_chunkhit("c1", "d1"), _fake_chunkhit("c2", "d1")]

    monkeypatch.setattr(
        "modal_workers.rag.hybrid_search.hybrid_search", stub_hs,
    )
    out = rag_handle.hybrid_search(sb, "PDUFA AXS-05", corpus="literature", k=2)
    assert isinstance(out, list) and len(out) == 2
    for row in out:
        assert set(row.keys()) >= {
            "chunk_id", "document_id", "chunk_text", "contextual_prefix",
            "section_path", "score", "rerank_score", "source", "title",
            "published_at",
        }
    assert out[0]["score"] == 0.5
    assert out[0]["rerank_score"] == 0.7


def test_hybrid_search_asset_id_resolves_to_document_ids(monkeypatch):
    """Passing asset_id triggers an asset_documents lookup; doc_ids are
    forwarded to the underlying hybrid_search."""
    from orchestrator_runtime import rag_handle

    sb = FakeSb({
        ("GET", "asset_documents"): [
            {"document_id": "doc-a"}, {"document_id": "doc-b"},
        ],
    })
    captured: Dict[str, Any] = {}

    def stub_hs(_sb, _query, _corpus, *, k=8,
                document_ids_filter=None, rerank=True):
        captured["doc_ids"] = document_ids_filter
        captured["corpus"] = _corpus
        captured["k"] = k
        return []

    monkeypatch.setattr(
        "modal_workers.rag.hybrid_search.hybrid_search", stub_hs,
    )
    rag_handle.hybrid_search(
        sb, "q", corpus="all", k=4, asset_id="asset-1",
    )
    assert captured["doc_ids"] == ["doc-a", "doc-b"]
    assert captured["corpus"] == "all"
    assert captured["k"] == 4


def test_hybrid_search_explicit_document_ids_take_precedence(monkeypatch):
    """document_ids passed in override asset_id-derived list."""
    from orchestrator_runtime import rag_handle

    sb = FakeSb({
        ("GET", "asset_documents"): [{"document_id": "doc-from-asset"}],
    })
    captured: Dict[str, Any] = {}

    def stub_hs(_sb, _query, _corpus, *, k=8,
                document_ids_filter=None, rerank=True):
        captured["doc_ids"] = document_ids_filter
        return []

    monkeypatch.setattr(
        "modal_workers.rag.hybrid_search.hybrid_search", stub_hs,
    )
    rag_handle.hybrid_search(
        sb, "q", corpus="all", asset_id="asset-1",
        document_ids=["explicit-doc"],
    )
    assert captured["doc_ids"] == ["explicit-doc"]


def test_get_chunk_with_neighbors_filters_to_window():
    """`with_neighbors=N` keeps only siblings within ±N of the chunk index."""
    from orchestrator_runtime import rag_handle

    chunk_row = {
        "id": "c5", "document_id": "d1", "chunk_index": 5,
        "chunk_text": "core", "contextual_prefix": None,
        "section_path": [], "parent_chunk_id": None,
    }
    siblings = [
        {"id": "c2", "chunk_index": 2, "chunk_text": "out-of-window",
         "contextual_prefix": None, "section_path": []},
        {"id": "c4", "chunk_index": 4, "chunk_text": "before",
         "contextual_prefix": None, "section_path": []},
        {"id": "c5", "chunk_index": 5, "chunk_text": "core",
         "contextual_prefix": None, "section_path": []},
        {"id": "c6", "chunk_index": 6, "chunk_text": "after",
         "contextual_prefix": None, "section_path": []},
    ]
    # Two GET calls: one for the chunk itself, one for siblings.
    calls = {"chunk": [chunk_row], "siblings": siblings}

    class _Sb:
        def _rest(self, method, path, **kwargs):
            params = kwargs.get("params", {}) or {}
            if "id" in params and params["id"].startswith("eq."):
                return calls["chunk"]
            return calls["siblings"]

    out = rag_handle.get_chunk(_Sb(), "c5", with_neighbors=1)
    assert "siblings" in out
    sib_idx = [s["chunk_index"] for s in out["siblings"]]
    assert sib_idx == [4, 5, 6]


def test_get_chunk_no_neighbors_omits_siblings_key():
    from orchestrator_runtime import rag_handle

    class _Sb:
        def _rest(self, *_a, **_kw):
            return [{
                "id": "c5", "document_id": "d1", "chunk_index": 5,
                "chunk_text": "x", "contextual_prefix": None,
                "section_path": [], "parent_chunk_id": None,
            }]

    out = rag_handle.get_chunk(_Sb(), "c5", with_neighbors=0)
    assert "siblings" not in out


def test_get_chunk_missing_returns_error():
    from orchestrator_runtime import rag_handle

    class _Sb:
        def _rest(self, *_a, **_kw):
            return []

    out = rag_handle.get_chunk(_Sb(), "missing")
    assert out["error"] == "chunk not found"
    assert out["chunk_id"] == "missing"


def test_format_chunks_for_prompt_renders_numbered_blocks():
    from orchestrator_runtime import rag_handle

    chunks = [
        {
            "chunk_id": "abcdef0123", "document_id": "0123abcdef",
            "chunk_text": "This is chunk one.",
            "contextual_prefix": "Prefix one.",
            "section_path": ["intro"], "score": 0.5,
            "source": "pubmed", "title": "Paper A",
            "published_at": "2024-06-15",
        },
        {
            "chunk_id": "fedcba9876", "document_id": "9876fedcba",
            "chunk_text": "Second chunk text.",
            "contextual_prefix": None,
            "section_path": [], "score": 0.4,
            "source": "edgar", "title": None,
            "published_at": None,
        },
    ]
    out = rag_handle.format_chunks_for_prompt(chunks)
    assert "[1] Paper A" in out
    assert "[2] edgar" in out
    # 8-char document/chunk slugs surfaced for citation walk.
    assert "[0123abcd/abcdef01]" in out
    assert "[9876fedc/fedcba98]" in out
    assert "Prefix one." in out
    assert "This is chunk one." in out


def test_format_chunks_for_prompt_truncates_long_text():
    from orchestrator_runtime import rag_handle

    long_text = "x" * 5000
    chunks = [{
        "chunk_id": "c", "document_id": "d", "chunk_text": long_text,
        "contextual_prefix": None, "section_path": [], "score": 1.0,
        "source": "pubmed", "title": "T", "published_at": "2024",
    }]
    out = rag_handle.format_chunks_for_prompt(chunks, char_cap=100)
    assert "…" in out
    assert len(out) < 500


def test_format_chunks_for_prompt_empty_returns_empty_string():
    from orchestrator_runtime import rag_handle
    assert rag_handle.format_chunks_for_prompt([]) == ""
