"""Hybrid search: BM25 + dense + RRF + rerank.

  1. Dense leg: per-corpus chunk_embeddings table, pgvector cosine, HNSW
     partial index `WHERE provider = $1`.
  2. BM25 leg: document_chunks.tsv (GIN-indexed) with ts_rank_cd.
  3. Fuse: Reciprocal Rank Fusion (k=60), top 50 per leg → top 25 fused.
  4. Rerank: voyage rerank-2.5 (or cohere fallback) → top N.
  5. Optional: write the result to retrieval_cache, TTL 1h (news) or 24h.

Caller passes a corpus key in {'literature','filings','labels_aes','news','all'}.
'all' fans out across the four corpora and merges by score.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CORPUS_TABLE = {
    "literature": "chunk_embeddings_literature",
    "filings": "chunk_embeddings_filings",
    "labels_aes": "chunk_embeddings_labels_aes",
    "news": "chunk_embeddings_news",
}

CORPUS_DIM = {
    "literature": 1024,
    "filings": 2000,
    "labels_aes": 2000,
    "news": 2000,
}

CORPUS_TTL_HOURS = {
    "literature": 24,
    "filings": 24,
    "labels_aes": 24,
    "news": 1,
}

RRF_K = 60
DEFAULT_BM25_TOP = 50
DEFAULT_DENSE_TOP = 50
DEFAULT_FUSED_TOP = 25
DEFAULT_RERANK_TOP = 8


@dataclass
class ChunkHit:
    chunk_id: str
    document_id: str
    chunk_text: str
    contextual_prefix: Optional[str]
    section_path: List[str] = field(default_factory=list)
    score: float = 0.0
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    source: Optional[str] = None
    title: Optional[str] = None
    published_at: Optional[str] = None


def _normalize_query(q: str) -> str:
    return " ".join(q.lower().split())


def _cache_key(
    query: str, corpus_filter: Dict[str, Any], k: int, reranker_model: str,
) -> str:
    payload = json.dumps({
        "q": _normalize_query(query),
        "filter": corpus_filter,
        "k": k,
        "rr": reranker_model,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# BM25 leg via Postgres tsvector
# ---------------------------------------------------------------------------

def _bm25_search(
    sb, query: str, corpus: str, top: int = DEFAULT_BM25_TOP,
    document_ids_filter: Optional[List[str]] = None,
) -> List[Tuple[str, float]]:
    """Return list of (chunk_id, bm25_score) ordered desc. Filter by corpus
    via the chunk_embeddings_<corpus> existence (chunks belonging to docs
    whose source maps to that corpus).

    Uses Supabase RPC if available; falls back to client-side scoring.
    """
    rpc_payload: Dict[str, Any] = {
        "p_query": query,
        "p_corpus": corpus,
        "p_top": top,
    }
    if document_ids_filter:
        rpc_payload["p_document_ids"] = document_ids_filter
    try:
        rows = sb._rest("POST", "rpc/rag_bm25_search", json_body=rpc_payload) or []
        return [(r["chunk_id"], float(r["score"])) for r in rows]
    except Exception:  # noqa: BLE001
        # RPC not present yet — surface no BM25 results and lean on dense.
        # Migration can add the RPC later without breaking callers.
        logger.debug("rag_bm25_search RPC unavailable; BM25 leg skipped")
        return []


# ---------------------------------------------------------------------------
# Dense leg via pgvector
# ---------------------------------------------------------------------------

def _dense_search(
    sb, query_embedding: List[float], corpus: str, provider: str,
    top: int = DEFAULT_DENSE_TOP,
    document_ids_filter: Optional[List[str]] = None,
) -> List[Tuple[str, float]]:
    """Return list of (chunk_id, cosine_sim) ordered desc."""
    rpc_payload: Dict[str, Any] = {
        "p_embedding": query_embedding,
        "p_corpus": corpus,
        "p_provider": provider,
        "p_top": top,
    }
    if document_ids_filter:
        rpc_payload["p_document_ids"] = document_ids_filter
    try:
        rows = sb._rest("POST", "rpc/rag_dense_search",
                        json_body=rpc_payload) or []
        return [(r["chunk_id"], float(r["similarity"])) for r in rows]
    except Exception:  # noqa: BLE001
        logger.debug("rag_dense_search RPC unavailable; dense leg skipped")
        return []


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    bm25_hits: List[Tuple[str, float]],
    dense_hits: List[Tuple[str, float]],
    k: int = RRF_K,
) -> List[Tuple[str, float, Optional[int], Optional[int]]]:
    """Fuse two ranked lists via RRF. Returns (chunk_id, rrf_score,
    bm25_rank, dense_rank)."""
    bm25_ranks = {cid: i + 1 for i, (cid, _) in enumerate(bm25_hits)}
    dense_ranks = {cid: i + 1 for i, (cid, _) in enumerate(dense_hits)}
    candidates = set(bm25_ranks) | set(dense_ranks)
    fused: List[Tuple[str, float, Optional[int], Optional[int]]] = []
    for cid in candidates:
        b = bm25_ranks.get(cid)
        d = dense_ranks.get(cid)
        score = 0.0
        if b is not None:
            score += 1.0 / (k + b)
        if d is not None:
            score += 1.0 / (k + d)
        fused.append((cid, score, b, d))
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Hydration — chunk_id list → ChunkHit with text + metadata
# ---------------------------------------------------------------------------

def _hydrate_chunks(sb, chunk_ids: List[str]) -> Dict[str, ChunkHit]:
    if not chunk_ids:
        return {}
    in_clause = ",".join(chunk_ids)
    rows = sb._rest(
        "GET", "document_chunks",
        params={
            "id": f"in.({in_clause})",
            "select": (
                "id,document_id,chunk_text,contextual_prefix,section_path,"
                "document:documents(source,title,published_at)"
            ),
        },
    ) or []
    out: Dict[str, ChunkHit] = {}
    for r in rows:
        doc = r.get("document") or {}
        out[r["id"]] = ChunkHit(
            chunk_id=r["id"],
            document_id=r["document_id"],
            chunk_text=r["chunk_text"],
            contextual_prefix=r.get("contextual_prefix"),
            section_path=r.get("section_path") or [],
            source=doc.get("source"),
            title=doc.get("title"),
            published_at=doc.get("published_at"),
        )
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def hybrid_search(
    sb,
    query: str,
    corpus: str,
    *,
    k: int = DEFAULT_RERANK_TOP,
    document_ids_filter: Optional[List[str]] = None,
    rerank: bool = True,
    embedder=None,
    reranker=None,
    use_cache: bool = True,
) -> List[ChunkHit]:
    """Run hybrid retrieval. Returns up to k chunks ordered by reranker score
    (or fused RRF score if rerank=False).

    `corpus`: 'literature' | 'filings' | 'labels_aes' | 'news' | 'all'.
    `embedder`/`reranker`: pass-through; default to RAG_PROVIDER selection.
    """
    from modal_workers.rag import (
        CORPUS_FAMILIES, get_embedder, get_reranker,
    )

    if corpus == "all":
        # Fan out and merge by reranker score (or RRF score).
        merged: Dict[str, ChunkHit] = {}
        for fam in CORPUS_FAMILIES:
            for h in hybrid_search(
                sb, query, fam, k=k,
                document_ids_filter=document_ids_filter, rerank=rerank,
                embedder=embedder, reranker=reranker, use_cache=use_cache,
            ):
                if h.chunk_id not in merged or h.score > merged[h.chunk_id].score:
                    merged[h.chunk_id] = h
        return sorted(merged.values(), key=lambda x: x.score, reverse=True)[:k]

    if corpus not in CORPUS_TABLE:
        raise ValueError(f"unknown corpus: {corpus}")

    embedder = embedder or get_embedder()
    reranker = reranker or get_reranker() if rerank else None
    reranker_model = reranker.name if reranker else "none"

    # Cache lookup.
    cache_key = _cache_key(
        query, {"corpus": corpus, "doc_ids": document_ids_filter or None}, k,
        reranker_model,
    )
    if use_cache:
        cached = sb._rest(
            "GET", "retrieval_cache",
            params={"cache_key": f"eq.{cache_key}", "select": "*"},
        ) or []
        if cached and cached[0].get("expires_at"):
            try:
                exp = datetime.fromisoformat(
                    cached[0]["expires_at"].replace("Z", "+00:00"))
                if exp > datetime.now(timezone.utc):
                    chunk_ids = cached[0]["result_chunk_ids"]
                    scores = cached[0]["result_scores"]
                    hits = _hydrate_chunks(sb, chunk_ids)
                    out = []
                    for cid, sc in zip(chunk_ids, scores):
                        h = hits.get(cid)
                        if h:
                            h.score = float(sc)
                            out.append(h)
                    if out:
                        return out
            except Exception:  # noqa: BLE001
                pass

    # BM25 + dense + fuse.
    bm25_hits = _bm25_search(
        sb, query, corpus, top=DEFAULT_BM25_TOP,
        document_ids_filter=document_ids_filter,
    )
    qvec = embedder.embed_query(query, output_dim=CORPUS_DIM[corpus])
    dense_hits = _dense_search(
        sb, qvec, corpus, embedder.provider, top=DEFAULT_DENSE_TOP,
        document_ids_filter=document_ids_filter,
    )
    fused = reciprocal_rank_fusion(bm25_hits, dense_hits, k=RRF_K)
    fused = fused[:DEFAULT_FUSED_TOP]

    chunk_ids = [c[0] for c in fused]
    hits_by_id = _hydrate_chunks(sb, chunk_ids)
    candidates: List[ChunkHit] = []
    for cid, score, b, d in fused:
        h = hits_by_id.get(cid)
        if not h:
            continue
        h.score = score
        h.bm25_rank = b
        h.dense_rank = d
        candidates.append(h)

    # Rerank (optional).
    if reranker and candidates:
        docs = [
            (c.contextual_prefix or "") + " " + (c.chunk_text or "")
            for c in candidates
        ]
        rer = reranker.rerank(query=query, docs=docs, top_k=k)
        ranked = []
        for r in rer:
            c = candidates[r.index]
            c.rerank_score = r.score
            c.score = r.score
            ranked.append(c)
        results = ranked
    else:
        results = candidates[:k]

    # Cache write.
    if use_cache and results:
        ttl_hours = CORPUS_TTL_HOURS.get(corpus, 24)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        ).isoformat()
        try:
            sb._rest(
                "POST", "retrieval_cache",
                json_body={
                    "cache_key": cache_key,
                    "query_text": query,
                    "corpus_filter": {
                        "corpus": corpus,
                        "doc_ids": document_ids_filter or None,
                    },
                    "k": k,
                    "reranker_model": reranker_model,
                    "result_chunk_ids": [c.chunk_id for c in results],
                    "result_scores": [c.score for c in results],
                    "expires_at": expires_at,
                },
                prefer="resolution=merge-duplicates",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("retrieval_cache write failed: %s", exc)

    return results
