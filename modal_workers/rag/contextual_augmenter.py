"""Contextual retrieval augmenter — Anthropic's pattern.

For each chunk, prepend a 50-100 token context string that situates it
within its parent document. The doc text goes into a `cache_control:
ephemeral` block; per-chunk requests reuse that cache, paying only the
cache_read rate (10x cheaper than uncached input).

Cost estimate (Haiku 4.5):
  - Per 50k-token doc with 80 chunks:
      cache write: 50k × $1.00/M  = $0.05
      cache reads: 80 × 50k × $0.10/M ≈ $0.40
      uncached I/O: 80 × (~50 in + ~80 out) × pricing ≈ $0.04
      total ≈ $0.49/doc → ~$6 per 1k chunks
  - Below the Haiku 4.5 200k context window for almost all docs; for super-
    long filings, sectional caching is used (one cache per Item-X).

The augmenter is best-effort: chunker stores `contextual_prefix=NULL`
initially; this function backfills, ordered by asset-link priority.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import anthropic

logger = logging.getLogger(__name__)

AUGMENTER_MODEL = "claude-haiku-4-5-20251001"
HAIKU_CONTEXT_TOKENS = 200_000
SAFE_DOC_TOKEN_BUDGET = 180_000  # leave room for system + per-chunk message
WINDOW_OVERLAP_TOKENS = 5_000

SYSTEM_PROMPT = (
    "You generate one-sentence context strings to situate a chunk within its "
    "parent document for retrieval. Output only the context line, no preamble, "
    "no quotes."
)


@dataclass
class AugmentResult:
    chunk_id: str
    context: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float


def _user_prompt_for_chunk(chunk_text: str) -> str:
    return (
        f"<chunk>\n{chunk_text}\n</chunk>\n\n"
        "Write a one-sentence context (≤30 words) that situates this chunk "
        "within the document above. Mention the section, the topic, and any "
        "key entity (drug, sponsor, NCT, filing date) needed for retrieval."
    )


def _build_messages_with_doc_cache(
    doc_text: str, chunk_text: str,
) -> List[Dict[str, Any]]:
    """Build a 2-block user message: cached doc + uncached per-chunk prompt."""
    return [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Document:\n\n{doc_text}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": _user_prompt_for_chunk(chunk_text),
            },
        ],
    }]


def augment_chunks_for_document(
    client: anthropic.Anthropic,
    doc_text: str,
    chunks: List[Dict[str, Any]],
) -> List[AugmentResult]:
    """Generate contextual prefixes for one document's chunks. The doc text is
    cached on the first call; subsequent calls hit the cache.

    `chunks` is a list of {id, chunk_text} dicts (id = document_chunks.id).
    """
    from orchestrator_runtime.pricing import estimate_cost

    if not chunks:
        return []
    out: List[AugmentResult] = []
    for c in chunks:
        chunk_id = c["id"]
        chunk_text = c.get("chunk_text") or ""
        try:
            resp = client.messages.create(
                model=AUGMENTER_MODEL,
                max_tokens=120,
                system=SYSTEM_PROMPT,
                messages=_build_messages_with_doc_cache(doc_text, chunk_text),
            )
        except anthropic.APIError as exc:
            logger.warning("Augmenter API error for chunk %s: %s", chunk_id, exc)
            time.sleep(1.0)
            continue
        text_out = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = resp.usage
        in_tok = usage.input_tokens
        out_tok = usage.output_tokens
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = estimate_cost(
            AUGMENTER_MODEL,
            input_tokens=in_tok, output_tokens=out_tok,
            cache_creation_tokens=cache_create, cache_read_tokens=cache_read,
        )
        out.append(AugmentResult(
            chunk_id=chunk_id,
            context=text_out,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_tokens=cache_create,
            cache_read_tokens=cache_read,
            cost_usd=cost,
        ))
    return out


def split_doc_into_windows(
    doc_text: str, window_tokens: int = SAFE_DOC_TOKEN_BUDGET,
    overlap_tokens: int = WINDOW_OVERLAP_TOKENS,
) -> List[str]:
    """For docs above SAFE_DOC_TOKEN_BUDGET, split into overlapping windows.
    Each window is augmented independently (separate cache entries)."""
    from modal_workers.rag.chunker import estimate_tokens
    if estimate_tokens(doc_text) <= window_tokens:
        return [doc_text]
    # Word-based windowing approximating the token budget.
    words = doc_text.split()
    words_per_window = int(window_tokens / 0.75)
    overlap_words = int(overlap_tokens / 0.75)
    out: List[str] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + words_per_window)
        out.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start = end - overlap_words
    return out


def write_contextual_prefix(
    sb, chunk_id: str, context: str,
) -> None:
    """PATCH document_chunks.contextual_prefix. Caller batches if needed."""
    sb._rest(
        "PATCH", "document_chunks",
        params={"id": f"eq.{chunk_id}"},
        json_body={"contextual_prefix": context},
    )


def augment_document(
    sb, client: anthropic.Anthropic, document_id: str,
) -> Dict[str, Any]:
    """End-to-end: fetch chunks for a document, augment, write back. Returns
    summary stats. Skips chunks that already have contextual_prefix set
    (idempotent)."""
    doc_rows = sb._rest(
        "GET", "documents",
        params={"id": f"eq.{document_id}", "select": "id,raw_text"},
    ) or []
    if not doc_rows:
        return {"error": "document not found", "document_id": document_id}
    doc = doc_rows[0]
    chunks = sb._rest(
        "GET", "document_chunks",
        params={
            "document_id": f"eq.{document_id}",
            "contextual_prefix": "is.null",
            "select": "id,chunk_text,chunk_index",
            "order": "chunk_index.asc",
        },
    ) or []
    if not chunks:
        return {
            "document_id": document_id,
            "augmented": 0, "total_cost_usd": 0.0,
        }

    results = augment_chunks_for_document(client, doc.get("raw_text") or "", chunks)
    for r in results:
        try:
            write_contextual_prefix(sb, r.chunk_id, r.context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Augmenter PATCH failed for %s: %s",
                           r.chunk_id, exc)
    return {
        "document_id": document_id,
        "augmented": len(results),
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
    }
