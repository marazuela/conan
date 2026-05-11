"""internal_rag_mcp — FastMCP server exposing the v3 RAG retrieval stack.

Tools (Plan §S5.3):
  - hybrid_search          : BM25 + dense + RRF + rerank over a corpus family
  - get_chunk              : fetch one chunk + N siblings
  - get_document_summary   : title + parent-level chunk roll-ups
  - get_citation_graph     : walk citation_graph_cache
  - verify_claim           : Sonnet judge over hybrid_search results

Sub-agents (literature, competitive, regulatory, ic_memo per D-107) call
these tools to retrieve from primary sources rather than hallucinating from
training data.

Run:
  pip install "mcp[cli]"
  python -m conan_fda_orchestrator_plugin.mcp_servers.internal_rag_mcp
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "internal_rag_mcp requires the `mcp` package with FastMCP support. "
        "Install with `pip install 'mcp[cli]'`."
    ) from exc

from modal_workers.shared.supabase_client import SupabaseClient


_sb: Optional[SupabaseClient] = None


def _client() -> SupabaseClient:
    global _sb
    if _sb is None:
        _sb = SupabaseClient()
    return _sb


mcp = FastMCP(
    name="conan-internal-rag",
    instructions=(
        "Retrieval over Conan's primary-source corpus (FDA filings, EDGAR, "
        "DailyMed, FAERS, ClinicalTrials.gov, PubMed). Use `hybrid_search` "
        "with the corpus family that matches your sub-agent role: "
        "'literature' for PubMed/preprints, 'filings' for EDGAR/FDA briefing, "
        "'labels_aes' for DailyMed/FAERS/warning letters, 'news' for press "
        "releases / Polygon news / CT.gov updates, or 'all' for cross-corpus."
    ),
)


@mcp.tool()
def hybrid_search(
    query: str,
    corpus: str = "all",
    k: int = 8,
    asset_id: Optional[str] = None,
    document_ids: Optional[List[str]] = None,
    rerank: bool = True,
) -> List[Dict[str, Any]]:
    """Run hybrid retrieval (BM25 + dense + RRF + rerank).

    Args:
        query: natural language query.
        corpus: 'literature' | 'filings' | 'labels_aes' | 'news' | 'all'.
        k: number of chunks to return (after rerank).
        asset_id: optional fda_asset id; restricts to documents linked to it.
        document_ids: optional explicit allowlist of document ids.
        rerank: whether to apply the cross-encoder reranker (default true).

    Returns a list of {chunk_id, document_id, chunk_text, contextual_prefix,
    section_path, score, source, title, published_at}.
    """
    from modal_workers.rag.hybrid_search import hybrid_search as _hs

    sb = _client()
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


@mcp.tool()
def get_chunk(chunk_id: str, with_neighbors: int = 0) -> Dict[str, Any]:
    """Fetch one chunk and (optionally) its N preceding/following siblings.

    Args:
        chunk_id: document_chunks.id.
        with_neighbors: 0..5; if >0 returns siblings within the same document.
    """
    sb = _client()
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
    lo = max(0, int(chunk["chunk_index"]) - with_neighbors)
    hi = int(chunk["chunk_index"]) + with_neighbors
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
    siblings = [s for s in siblings if int(s["chunk_index"]) <= hi]
    chunk["siblings"] = siblings
    return chunk


@mcp.tool()
def get_document_summary(document_id: str) -> Dict[str, Any]:
    """Return document metadata + parent-level chunk roll-ups (avoids
    dragging full raw_text through tool I/O).
    """
    sb = _client()
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


@mcp.tool()
def get_citation_graph(
    document_id: str, depth: int = 1, direction: str = "both",
) -> Dict[str, Any]:
    """BFS walk citation_graph_cache.

    Args:
        document_id: starting node.
        depth: 1..3.
        direction: 'out' | 'in' | 'both'.
    """
    from modal_workers.rag.citation_graph import (
        get_citation_graph as _walk,
    )

    sb = _client()
    return _walk(sb, document_id, depth=max(1, min(3, depth)),
                 direction=direction)


@mcp.tool()
def verify_claim(
    claim: str,
    corpus: str = "all",
    k: int = 12,
) -> Dict[str, Any]:
    """Verify a factual claim against the corpus. Runs hybrid_search, then
    asks Sonnet to judge whether the claim follows from the retrieved chunks.

    Returns {status, evidence_chunks, confidence, reasoning}. Status:
      'supported'   — claim is substantiated
      'contradicted'— evidence contradicts the claim
      'insufficient'— not enough evidence in the corpus
    """
    from modal_workers.rag.hybrid_search import hybrid_search as _hs
    import anthropic

    sb = _client()
    hits = _hs(sb, claim, corpus, k=k, rerank=True)
    if not hits:
        return {
            "status": "insufficient",
            "evidence_chunks": [],
            "confidence": 0.0,
            "reasoning": "no matching chunks",
        }
    evidence = "\n\n".join(
        f"[{i + 1}] {h.title or h.source} ({h.published_at}):\n"
        f"{h.contextual_prefix or ''}\n{h.chunk_text}"
        for i, h in enumerate(hits)
    )
    judge_prompt = (
        f"Claim:\n{claim}\n\n"
        f"Evidence (numbered):\n{evidence}\n\n"
        "Decide whether the claim follows from the evidence. Output JSON:\n"
        '{"status": "supported|contradicted|insufficient", '
        '"confidence": 0.0-1.0, "reasoning": "<≤300 chars>", '
        '"key_evidence_indices": [<int>, ...]}'
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=512,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    text_out = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text_out.startswith("```"):
        import re
        text_out = re.sub(r"^```(?:json)?\s*\n?", "", text_out)
        text_out = re.sub(r"\n?```\s*$", "", text_out)
    import json as _json
    try:
        parsed = _json.loads(text_out)
    except _json.JSONDecodeError:
        parsed = {"status": "insufficient", "confidence": 0.0,
                  "reasoning": "judge output unparseable"}
    parsed["evidence_chunks"] = [
        {
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "title": h.title,
            "source": h.source,
            "score": round(h.score, 4),
        }
        for h in hits
    ]
    return parsed


if __name__ == "__main__":
    mcp.run()
