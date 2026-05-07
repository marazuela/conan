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
# Voyage Matryoshka truncation: 2000-dim corpus storage but Voyage API only
# accepts {256, 512, 1024, 2048}. The embedder asks for 2048 then truncates
# to the requested dim locally. Regression-locks the migration's vector(2000)
# choice (pgvector HNSW caps at 2000 dims).
# ---------------------------------------------------------------------------

class _FakeVoyageClient:
    """Stand-in for voyageai.Client that records every embed() call and
    returns 2048-dim vectors of zeros."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, model, input_type, output_dimension=None):
        self.calls.append({
            "texts": texts, "model": model,
            "input_type": input_type,
            "output_dimension": output_dimension,
        })
        # Voyage returns the dim it was asked for. We assert below that the
        # embedder asks for 2048 (the nearest native dim) when caller wants 2000.
        api_dim = output_dimension or 2048
        return _FakeVoyageResult([[0.0] * api_dim for _ in texts])


class _FakeVoyageResult:
    def __init__(self, embeddings):
        self.embeddings = embeddings


def test_voyage_embedder_matryoshka_truncates_to_2000():
    """When caller asks for output_dim=2000 (the corpus storage dim), the
    embedder must request 2048 from Voyage (a native dim) and truncate to
    2000 locally. Locks the post-2026-05-07 design note."""
    e = VoyageEmbedder(api_key="dummy")
    fake = _FakeVoyageClient()
    e._client = fake  # bypass lazy init

    out = e.embed_documents(["hello", "world"], output_dim=2000)

    assert len(fake.calls) == 1
    # API was asked for 2048, not 2000, because 2000 is not a Voyage native dim
    assert fake.calls[0]["output_dimension"] == 2048
    # Output vectors are truncated to 2000 dims locally
    assert len(out) == 2
    assert all(len(v) == 2000 for v in out)


def test_voyage_embedder_native_dim_passthrough():
    """When caller asks for a Voyage native dim (1024 for literature),
    no local truncation happens — the API call uses that dim directly."""
    e = VoyageEmbedder(api_key="dummy")
    fake = _FakeVoyageClient()
    e._client = fake

    out = e.embed_documents(["x"], output_dim=1024)

    assert fake.calls[0]["output_dimension"] == 1024
    assert len(out[0]) == 1024


def test_voyage_embedder_default_dim_when_unspecified():
    """When caller passes output_dim=None, the embedder leaves
    output_dimension off (Voyage defaults to its model-native dim)."""
    e = VoyageEmbedder(api_key="dummy")
    fake = _FakeVoyageClient()
    e._client = fake

    e.embed_documents(["x"], output_dim=None)
    assert fake.calls[0]["output_dimension"] is None


def test_voyage_embed_query_uses_query_input_type():
    """embed_query must use input_type='query' (not 'document') so Voyage
    applies the asymmetric instruct prefix correctly."""
    e = VoyageEmbedder(api_key="dummy")
    fake = _FakeVoyageClient()
    e._client = fake

    e.embed_query("a question", output_dim=1024)
    assert fake.calls[0]["input_type"] == "query"
    assert fake.calls[0]["texts"] == ["a question"]


def test_voyage_embed_documents_uses_document_input_type():
    e = VoyageEmbedder(api_key="dummy")
    fake = _FakeVoyageClient()
    e._client = fake

    e.embed_documents(["doc-1", "doc-2"], output_dim=1024)
    assert fake.calls[0]["input_type"] == "document"


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
