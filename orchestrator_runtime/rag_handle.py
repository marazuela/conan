"""Runtime-side RAG entry point.

Wraps `modal_workers.rag.hybrid_search` + the document/chunk/citation helpers
so the orchestrator runtime (Stage 1, sub-agents) can retrieve from the
primary-source corpus via direct Python calls — no FastMCP / subprocess on
the critical path. The MCP wrapper at
`conan-fda-orchestrator-plugin/mcp_servers/internal_rag_mcp.py` shares the
same underlying functions so Cowork bulk and operator-triggered tool use
get an identical surface (mirrors the D-114 pattern of `compute.py` having
both a runtime and an MCP entry point).

Public API:
    hybrid_search(sb, query, *, corpus="all", k=8, asset_id=None,
                  document_ids=None, rerank=True) -> list[dict]
    get_chunk(sb, chunk_id, *, with_neighbors=0) -> dict
    get_document_summary(sb, document_id) -> dict
    get_citation_graph(sb, document_id, *, depth=1, direction="both") -> dict

Each function returns plain JSON-shaped dicts (no dataclasses) so callers can
hand the result straight into a tool-use response without conversion.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def hybrid_search(
    sb,
    query: str,
    *,
    corpus: str = "all",
    k: int = 8,
    asset_id: Optional[str] = None,
    document_ids: Optional[List[str]] = None,
    rerank: bool = True,
) -> List[Dict[str, Any]]:
    """Run hybrid retrieval (BM25 + dense + RRF + rerank).

    Args:
        sb: SupabaseClient (or anything with `_rest`).
        query: natural-language query.
        corpus: 'literature' | 'filings' | 'labels_aes' | 'news' | 'all'.
        k: number of chunks to return after rerank.
        asset_id: optional fda_asset id; restricts to documents linked via
            `asset_documents.is_material = true`.
        document_ids: optional explicit allowlist (takes precedence over
            asset_id-derived list).
        rerank: whether to apply the cross-encoder reranker.

    Returns: list of dicts, each {chunk_id, document_id, chunk_text,
        contextual_prefix, section_path, score, rerank_score, source, title,
        published_at}.
    """
    from modal_workers.rag.hybrid_search import hybrid_search as _hs

    doc_ids = document_ids
    if asset_id and not doc_ids:
        rows = sb._rest(
            "GET", "asset_documents",
            params={
                "asset_id": f"eq.{asset_id}",
                "is_material": "is.true",
                "select": "document_id",
            },
        ) or []
        doc_ids = [r["document_id"] for r in rows]
    hits = _hs(
        sb, query, corpus,
        k=k, document_ids_filter=doc_ids, rerank=rerank,
    )
    return [
        {
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "chunk_text": h.chunk_text,
            "contextual_prefix": h.contextual_prefix,
            "section_path": h.section_path,
            "score": round(h.score, 4),
            "rerank_score": (
                round(h.rerank_score, 4) if h.rerank_score is not None else None
            ),
            "source": h.source,
            "title": h.title,
            "published_at": h.published_at,
        }
        for h in hits
    ]


def get_chunk(
    sb, chunk_id: str, *, with_neighbors: int = 0,
) -> Dict[str, Any]:
    """Fetch one chunk and (optionally) N preceding/following siblings."""
    rows = sb._rest(
        "GET", "document_chunks",
        params={
            "id": f"eq.{chunk_id}",
            "select": (
                "id,document_id,chunk_index,chunk_text,contextual_prefix,"
                "section_path,parent_chunk_id"
            ),
        },
    ) or []
    if not rows:
        return {"error": "chunk not found", "chunk_id": chunk_id}
    chunk = rows[0]
    if with_neighbors <= 0:
        return chunk
    n = max(0, min(5, int(with_neighbors)))
    lo = max(0, int(chunk["chunk_index"]) - n)
    hi = int(chunk["chunk_index"]) + n
    siblings = sb._rest(
        "GET", "document_chunks",
        params={
            "document_id": f"eq.{chunk['document_id']}",
            "chunk_index": f"gte.{lo}",
            "select": (
                "id,chunk_index,chunk_text,contextual_prefix,section_path"
            ),
            "order": "chunk_index.asc",
        },
    ) or []
    chunk["siblings"] = [
        s for s in siblings if lo <= int(s["chunk_index"]) <= hi
    ]
    return chunk


def get_document_summary(sb, document_id: str) -> Dict[str, Any]:
    """Document metadata + parent-level chunk roll-ups."""
    docs = sb._rest(
        "GET", "documents",
        params={
            "id": f"eq.{document_id}",
            "select": "id,source,doc_type,title,published_at,url",
        },
    ) or []
    if not docs:
        return {"error": "document not found", "document_id": document_id}
    parents = sb._rest(
        "GET", "document_chunks",
        params={
            "document_id": f"eq.{document_id}",
            "extensions->>role": "eq.parent",
            "select": "id,chunk_text,section_path",
            "order": "chunk_index.asc",
        },
    ) or []
    out = dict(docs[0])
    out["section_summaries"] = [
        {
            "chunk_id": p["id"],
            "section_path": p.get("section_path") or [],
            "summary": (p.get("chunk_text") or "")[:600],
        }
        for p in parents
    ]
    return out


def get_citation_graph(
    sb, document_id: str, *, depth: int = 1, direction: str = "both",
) -> Dict[str, Any]:
    """BFS walk of `citation_graph_cache` from a starting document."""
    from modal_workers.rag.citation_graph import (
        get_citation_graph as _walk,
    )

    return _walk(
        sb, document_id,
        depth=max(1, min(3, int(depth))),
        direction=direction,
    )


def format_chunks_for_prompt(
    chunks: List[Dict[str, Any]], *, char_cap: int = 2400,
) -> str:
    """Render hybrid_search results as a numbered `<context>` block suitable
    for splicing into a Stage 1 prompt prefix.

    Each chunk is rendered as:
        [n] <title or source> (<published_at>) [<doc:8>/<chunk:8>]
        <contextual_prefix>
        <chunk_text — truncated to char_cap>

    The 8-char id slugs let the constitutional check (Stage 7) walk the
    citation back to the underlying document/chunk row.
    """
    if not chunks:
        return ""
    lines: List[str] = []
    for i, h in enumerate(chunks, start=1):
        head = (
            f"[{i}] {h.get('title') or h.get('source') or '(untitled)'} "
            f"({h.get('published_at') or 'n/d'}) "
            f"[{(h.get('document_id') or '')[:8]}/{(h.get('chunk_id') or '')[:8]}]"
        )
        prefix = (h.get("contextual_prefix") or "").strip()
        text = (h.get("chunk_text") or "").strip()
        if char_cap and len(text) > char_cap:
            text = text[: char_cap - 1].rstrip() + "…"
        block = head
        if prefix:
            block += f"\n{prefix}"
        if text:
            block += f"\n{text}"
        lines.append(block)
    return "\n\n".join(lines)
