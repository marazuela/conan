"""Embedder Protocol + Voyage and OpenAI implementations.

Single config point: RAG_PROVIDER env (read in modal_workers.rag.__init__).
Both providers implement the same Protocol so chunker, hybrid_search, and
augmenter never reference vendor-specific code.

Matryoshka truncation:
  - Voyage voyage-3-large supports `output_dimension` natively (256/512/1024/2048).
  - OpenAI text-embedding-3-large supports `dimensions` parameter.

Both are passed `output_dim` from the corpus family (1024 for literature,
2000 elsewhere — see CORPUS_DIM in the package __init__). 2000 instead of
the providers' native 2048 because pgvector HNSW caps at 2000 dims; both
providers Matryoshka-truncate cleanly (2048→2000 retains ~99% of ranking).
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Embedder(Protocol):
    """Embedder interface. Implementations live below."""

    name: str
    provider: str  # 'voyage' | 'openai'
    default_dim: int

    def embed_documents(
        self, texts: List[str], output_dim: Optional[int] = None,
    ) -> List[List[float]]:
        ...

    def embed_query(
        self, text: str, output_dim: Optional[int] = None,
    ) -> List[float]:
        ...


# ---------------------------------------------------------------------------
# Voyage AI — primary provider
# ---------------------------------------------------------------------------

class VoyageEmbedder:
    """voyage-3-large embedder. Reads VOYAGE_API_KEY env."""

    name = "voyage-3-large"
    provider = "voyage"
    default_dim = 2000

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not self._api_key:
            logger.warning(
                "VOYAGE_API_KEY not set — VoyageEmbedder will fail on call. "
                "Set RAG_PROVIDER=openai_cohere to fall back."
            )
        self._client = None  # lazy

    def _get_client(self):
        if self._client is None:
            import voyageai
            self._client = voyageai.Client(api_key=self._api_key)
        return self._client

    # Voyage voyage-3-large natively supports output_dimension in
    # {256, 512, 1024, 2048}. The corpus storage tables use vector(2000)
    # (pgvector HNSW caps at 2000 dims), so when the caller requests 2000
    # we must ask Voyage for 2048 and Matryoshka-truncate to 2000 locally.
    # voyage-3-large is Matryoshka-trained, so 2048→2000 retains ~99% of
    # ranking quality (per migration 20260510000000 design note).
    _VOYAGE_NATIVE_DIMS = (256, 512, 1024, 2048)

    def _embed(
        self, texts: List[str], input_type: str, output_dim: Optional[int],
    ) -> List[List[float]]:
        client = self._get_client()
        kwargs = {
            "texts": texts,
            "model": self.name,
            "input_type": input_type,
        }
        api_dim = output_dim
        truncate_to: Optional[int] = None
        if output_dim is not None and output_dim not in self._VOYAGE_NATIVE_DIMS:
            api_dim = 2048
            truncate_to = output_dim
        if api_dim is not None:
            kwargs["output_dimension"] = api_dim
        result = client.embed(**kwargs)
        embeddings = result.embeddings
        if truncate_to is not None:
            embeddings = [vec[:truncate_to] for vec in embeddings]
        return embeddings

    def embed_documents(
        self, texts: List[str], output_dim: Optional[int] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        return self._embed(texts, input_type="document", output_dim=output_dim)

    def embed_query(
        self, text: str, output_dim: Optional[int] = None,
    ) -> List[float]:
        result = self._embed([text], input_type="query", output_dim=output_dim)
        return result[0]


# ---------------------------------------------------------------------------
# OpenAI — fallback provider
# ---------------------------------------------------------------------------

class OpenAIEmbedder:
    """text-embedding-3-large embedder. Reads OPENAI_API_KEY env. Supports
    `dimensions` parameter natively (Matryoshka)."""

    name = "text-embedding-3-large"
    provider = "openai"
    default_dim = 2000

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            logger.warning(
                "OPENAI_API_KEY not set — OpenAIEmbedder will fail on call."
            )
        self._client = None  # lazy

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    def _embed(
        self, texts: List[str], output_dim: Optional[int],
    ) -> List[List[float]]:
        client = self._get_client()
        kwargs = {"model": self.name, "input": texts}
        if output_dim is not None:
            kwargs["dimensions"] = output_dim
        resp = client.embeddings.create(**kwargs)
        return [d.embedding for d in resp.data]

    def embed_documents(
        self, texts: List[str], output_dim: Optional[int] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        return self._embed(texts, output_dim=output_dim)

    def embed_query(
        self, text: str, output_dim: Optional[int] = None,
    ) -> List[float]:
        return self._embed([text], output_dim=output_dim)[0]
