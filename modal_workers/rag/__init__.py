"""Conan v3 RAG package — Phase 4.5 contextual retrieval.

Components:
  - chunker:               section-aware hierarchical chunking
  - contextual_augmenter:  Haiku-4.5 contextual prefix backfill
  - embedder:              Voyage primary, OpenAI fallback (Protocol-based)
  - reranker:              Voyage primary, Cohere fallback (Protocol-based)
  - hybrid_search:         BM25 + dense + RRF + rerank
  - citation_graph:        doc-to-doc edge cache
  - eval:                  RAGAS gate (Modal scheduled function)

Provider selection: RAG_PROVIDER env in {'voyage', 'openai_cohere'}.
Default 'voyage' once Open Decision #8 lands; falls back to 'openai_cohere'
without code changes.
"""
from __future__ import annotations

import os

# Provider selection — single config point.
RAG_PROVIDER = os.environ.get("RAG_PROVIDER", "voyage")


def get_embedder():
    """Return the active Embedder instance based on RAG_PROVIDER env."""
    from modal_workers.rag.embedder import (
        OpenAIEmbedder, VoyageEmbedder,
    )
    if RAG_PROVIDER == "openai_cohere":
        return OpenAIEmbedder()
    return VoyageEmbedder()


def get_reranker():
    """Return the active Reranker instance based on RAG_PROVIDER env."""
    from modal_workers.rag.reranker import (
        CohereReranker, VoyageReranker,
    )
    if RAG_PROVIDER == "openai_cohere":
        return CohereReranker()
    return VoyageReranker()


# Corpus mapping — used by hybrid_search to pick the right embeddings table.
SOURCE_TO_CORPUS = {
    "pubmed": "literature",
    "biorxiv": "literature",
    "medrxiv": "literature",
    "edgar": "filings",
    "federal_register": "filings",
    "fda_advisory": "filings",
    "dailymed": "labels_aes",
    "faers": "labels_aes",
    "openfda": "labels_aes",
    "fda_warning_letter": "labels_aes",
    "fda_483": "labels_aes",
    "polygon_news": "news",
    "press_release": "news",
    "clinicaltrials": "news",
}

CORPUS_FAMILIES = ("literature", "filings", "labels_aes", "news")

# Embedding dimensions per corpus family (Matryoshka 1024 for literature,
# 2048 elsewhere). Voyage and OpenAI both honor these via their dims param.
CORPUS_DIM = {
    "literature": 1024,
    "filings": 2048,
    "labels_aes": 2048,
    "news": 2048,
}
