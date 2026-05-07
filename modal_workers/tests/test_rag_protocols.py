"""Tests for Embedder / Reranker Protocol conformance.

Pure structural — no live API calls. Live integration tests live in
test_rag_embedder_live.py and are env-gated.

Run: python -m pytest modal_workers/tests/test_rag_protocols.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

from modal_workers.rag.embedder import Embedder, OpenAIEmbedder, VoyageEmbedder
from modal_workers.rag.reranker import (
    CohereReranker, RerankResult, Reranker, VoyageReranker,
)


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

def test_voyage_embedder_implements_protocol():
    e = VoyageEmbedder(api_key="dummy")
    assert isinstance(e, Embedder)
    assert e.name == "voyage-3-large"
    assert e.provider == "voyage"
    assert e.default_dim == 2000


def test_openai_embedder_implements_protocol():
    e = OpenAIEmbedder(api_key="dummy")
    assert isinstance(e, Embedder)
    assert e.name == "text-embedding-3-large"
    assert e.provider == "openai"
    assert e.default_dim == 2000


def test_voyage_embedder_lazy_client_init():
    e = VoyageEmbedder(api_key="dummy")
    assert e._client is None  # not initialized at construction


def test_openai_embedder_empty_input_returns_empty():
    e = OpenAIEmbedder(api_key="dummy")
    # empty list short-circuits before client init
    assert e.embed_documents([]) == []


def test_voyage_embedder_empty_input_returns_empty():
    e = VoyageEmbedder(api_key="dummy")
    assert e.embed_documents([]) == []


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

def test_voyage_reranker_implements_protocol():
    r = VoyageReranker(api_key="dummy")
    assert isinstance(r, Reranker)
    assert r.name == "rerank-2.5"
    assert r.provider == "voyage"


def test_cohere_reranker_implements_protocol():
    r = CohereReranker(api_key="dummy")
    assert isinstance(r, Reranker)
    assert r.name == "rerank-3.5"
    assert r.provider == "cohere"


def test_reranker_empty_docs_returns_empty():
    assert VoyageReranker(api_key="dummy").rerank("q", [], top_k=5) == []
    assert CohereReranker(api_key="dummy").rerank("q", [], top_k=5) == []


def test_rerank_result_dataclass():
    r = RerankResult(index=0, score=0.95)
    assert r.index == 0
    assert r.score == 0.95


# ---------------------------------------------------------------------------
# Provider factory selection (RAG_PROVIDER env)
# ---------------------------------------------------------------------------

def test_get_embedder_voyage_default():
    os.environ["RAG_PROVIDER"] = "voyage"
    # Re-import to pick up env change.
    import importlib
    import modal_workers.rag as rag_pkg
    importlib.reload(rag_pkg)
    e = rag_pkg.get_embedder()
    assert e.provider == "voyage"


def test_get_embedder_openai_fallback():
    os.environ["RAG_PROVIDER"] = "openai_cohere"
    import importlib
    import modal_workers.rag as rag_pkg
    importlib.reload(rag_pkg)
    e = rag_pkg.get_embedder()
    assert e.provider == "openai"
    r = rag_pkg.get_reranker()
    assert r.provider == "cohere"
    # Restore default for downstream tests
    os.environ["RAG_PROVIDER"] = "voyage"
    importlib.reload(rag_pkg)


def test_corpus_dim_lookup():
    from modal_workers.rag import CORPUS_DIM
    assert CORPUS_DIM["literature"] == 1024
    assert CORPUS_DIM["filings"] == 2000
    assert CORPUS_DIM["labels_aes"] == 2000
    assert CORPUS_DIM["news"] == 2000


def test_source_to_corpus_mapping_complete():
    from modal_workers.rag import CORPUS_FAMILIES, SOURCE_TO_CORPUS
    # Every mapped value is one of the four families.
    for src, corpus in SOURCE_TO_CORPUS.items():
        assert corpus in CORPUS_FAMILIES, f"{src} maps to invalid {corpus}"
