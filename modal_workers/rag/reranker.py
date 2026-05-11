"""Reranker Protocol + Voyage and Cohere implementations.

Used after RRF fusion to re-score the fused top-25 down to top-N. Two impls
let RAG_PROVIDER swap without touching call sites.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """Reranker score for one document. `index` is the position in the
    original docs list passed to rerank()."""
    index: int
    score: float


@runtime_checkable
class Reranker(Protocol):
    """Reranker interface."""

    name: str
    provider: str  # 'voyage' | 'cohere'

    def rerank(
        self, query: str, docs: List[str], top_k: int,
    ) -> List[RerankResult]:
        ...


# ---------------------------------------------------------------------------
# Voyage AI — rerank-2.5
# ---------------------------------------------------------------------------

class VoyageReranker:
    name = "rerank-2.5"
    provider = "voyage"

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not self._api_key:
            logger.warning(
                "VOYAGE_API_KEY not set — VoyageReranker will fail on call."
            )
        self._client = None

    def _get_client(self):
        if self._client is None:
            import voyageai
            self._client = voyageai.Client(api_key=self._api_key)
        return self._client

    def rerank(
        self, query: str, docs: List[str], top_k: int,
    ) -> List[RerankResult]:
        if not docs:
            return []
        client = self._get_client()
        result = client.rerank(
            query=query,
            documents=docs,
            model=self.name,
            top_k=top_k,
        )
        return [
            RerankResult(index=r.index, score=float(r.relevance_score))
            for r in result.results
        ]


# ---------------------------------------------------------------------------
# Cohere — rerank-3.5
# ---------------------------------------------------------------------------

class CohereReranker:
    name = "rerank-3.5"
    provider = "cohere"

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("COHERE_API_KEY")
        if not self._api_key:
            logger.warning(
                "COHERE_API_KEY not set — CohereReranker will fail on call."
            )
        self._client = None

    def _get_client(self):
        if self._client is None:
            import cohere
            self._client = cohere.Client(api_key=self._api_key)
        return self._client

    def rerank(
        self, query: str, docs: List[str], top_k: int,
    ) -> List[RerankResult]:
        if not docs:
            return []
        client = self._get_client()
        resp = client.rerank(
            query=query,
            documents=docs,
            top_n=top_k,
            model=self.name,
        )
        return [
            RerankResult(index=r.index, score=float(r.relevance_score))
            for r in resp.results
        ]
