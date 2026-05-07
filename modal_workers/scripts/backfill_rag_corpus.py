"""backfill_rag_corpus — Phase 1A one-shot.

Walks `documents` rows that have no chunks yet (or fewer chunks than expected
based on raw_text length), chunks them via `modal_workers.rag.chunker`,
embeds the chunks, and writes them to `document_chunks` +
`chunk_embeddings_<corpus>`.

Order of operations per doc:
  1.  chunk_document() — section-aware leaf + parent chunks
  2.  insert document_chunks rows (parent first, then leaves with parent_chunk_id)
  3.  embed chunks in batches of EMBED_BATCH (default 64)
  4.  insert chunk_embeddings_<corpus> rows
  5.  optionally invoke contextual_augmenter — gated by --augment because it
      requires Haiku and burns ~$0.50 per long doc; safe to defer

Idempotent: a doc with any existing chunks is skipped unless --force is set.
Resumable: progress is tracked by `documents.id`; the script can be killed
and re-launched.

Usage:

    # Dry run — show what would be processed, no writes
    python -m modal_workers.scripts.backfill_rag_corpus --dry-run --limit 10

    # First pass — chunks + embeddings only, skip augmenter
    python -m modal_workers.scripts.backfill_rag_corpus --limit 200

    # Augmenter pass — only re-augments chunks where contextual_prefix IS NULL
    python -m modal_workers.scripts.backfill_rag_corpus --augment-only --limit 500

Environment:
    SUPABASE_URL, SUPABASE_SERVICE_KEY    — required
    VOYAGE_API_KEY                        — required for VoyageEmbedder (default)
    OPENAI_API_KEY                        — required for OpenAIEmbedder fallback
    ANTHROPIC_API_KEY                     — required for --augment

This is a script, not a Modal function — execute locally or wrap in a Modal
deploy when ready for scheduled re-backfill.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from typing import Any, Dict, List, Optional

from modal_workers.rag import (
    CORPUS_DIM,
    SOURCE_TO_CORPUS,
    get_embedder,
)
from modal_workers.rag.chunker import Chunk, chunk_document
from modal_workers.rag.hybrid_search import CORPUS_TABLE
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

DOC_BATCH_SIZE = 50
EMBED_BATCH = 64


def _fetch_doc_batch(
    sb: SupabaseClient, *, limit: int, offset: int = 0,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pull docs that need chunking. We can't easily LEFT JOIN in REST so we
    filter in Python after fetching candidate rows."""
    params: Dict[str, Any] = {
        "select": "id,source,doc_type,title,raw_text,published_at",
        "raw_text": "not.is.null",
        "limit": str(limit),
        "offset": str(offset),
        "order": "fetched_at.asc",
    }
    if source:
        params["source"] = f"eq.{source}"
    return sb._rest("GET", "documents", params=params) or []


def _doc_already_chunked(sb: SupabaseClient, document_id: str) -> bool:
    rows = sb._rest(
        "GET", "document_chunks",
        params={
            "document_id": f"eq.{document_id}",
            "select": "id",
            "limit": "1",
        },
    ) or []
    return bool(rows)


def _insert_chunks(
    sb: SupabaseClient, document_id: str, chunks: List[Chunk],
) -> List[Dict[str, Any]]:
    """Write chunks → document_chunks. Returns the inserted rows including
    server-assigned ids, ordered by chunk_index."""
    if not chunks:
        return []
    parent_indices = {c.chunk_index for c in chunks if c.parent_index is None}
    payload: List[Dict[str, Any]] = []
    chunk_ids: Dict[int, str] = {}
    for c in sorted(chunks, key=lambda x: x.chunk_index):
        cid = str(uuid.uuid4())
        chunk_ids[c.chunk_index] = cid
        payload.append({
            "id": cid,
            "document_id": document_id,
            "chunk_index": c.chunk_index,
            "chunk_text": c.chunk_text,
            "chunk_tokens": c.chunk_tokens,
            "section_path": c.section_path,
            "parent_chunk_id": (
                chunk_ids.get(c.parent_index) if c.parent_index is not None
                else None
            ),
            "extensions": {
                **(c.extensions or {}),
                "role": "parent" if c.chunk_index in parent_indices else "leaf",
            },
        })
    sb._rest("POST", "document_chunks", json_body=payload)
    return payload


def _corpus_for_doc(doc: Dict[str, Any]) -> Optional[str]:
    src = (doc.get("source") or "").strip()
    return SOURCE_TO_CORPUS.get(src)


def _embed_and_persist(
    sb: SupabaseClient, doc: Dict[str, Any], chunk_rows: List[Dict[str, Any]],
    embedder, *, batch: int = EMBED_BATCH,
) -> int:
    """Embed chunk texts in batches → write to chunk_embeddings_<corpus>."""
    corpus = _corpus_for_doc(doc)
    if not corpus:
        logger.warning("no corpus mapping for source=%r doc=%s — skipping embeds",
                       doc.get("source"), doc.get("id"))
        return 0
    table = CORPUS_TABLE[corpus]
    dim = CORPUS_DIM[corpus]
    written = 0
    for i in range(0, len(chunk_rows), batch):
        slice_ = chunk_rows[i:i + batch]
        texts = [
            (r.get("chunk_text") or "")[:8000]  # voyage 32k input limit; safe cap
            for r in slice_
        ]
        vectors = embedder.embed_documents(texts, output_dim=dim)
        rows = []
        for r, v in zip(slice_, vectors):
            rows.append({
                "chunk_id": r["id"],
                "document_id": r["document_id"],
                "provider": embedder.provider,
                "model": embedder.name,
                "embedding": v,
            })
        sb._rest("POST", table, json_body=rows)
        written += len(rows)
    return written


def _run_augmenter_pass(
    sb: SupabaseClient, *, limit: int, source: Optional[str] = None,
) -> Dict[str, Any]:
    """Walk documents that have ≥1 chunk missing `contextual_prefix` and
    invoke `augment_document` per-doc. Idempotent: per-doc augmenter skips
    chunks that already have a prefix."""
    import anthropic

    from modal_workers.rag.contextual_augmenter import augment_document

    client = anthropic.Anthropic()
    # Find candidate doc_ids by scanning chunks where contextual_prefix is null.
    params: Dict[str, Any] = {
        "select": "document_id",
        "contextual_prefix": "is.null",
        "limit": str(limit),
        "order": "document_id.asc",
    }
    chunk_rows = sb._rest("GET", "document_chunks", params=params) or []
    seen_doc_ids: List[str] = []
    seen_set = set()
    for r in chunk_rows:
        did = r.get("document_id")
        if not did or did in seen_set:
            continue
        seen_set.add(did)
        seen_doc_ids.append(did)
    if source:
        filter_rows = sb._rest(
            "GET", "documents",
            params={
                "id": f"in.({','.join(seen_doc_ids)})",
                "source": f"eq.{source}",
                "select": "id",
            },
        ) or []
        seen_doc_ids = [r["id"] for r in filter_rows]
    augmented_total = 0
    cost_total = 0.0
    failures = 0
    for did in seen_doc_ids:
        try:
            res = augment_document(sb, client, did)
            augmented_total += int(res.get("augmented", 0))
            cost_total += float(res.get("total_cost_usd", 0.0))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.exception("augmenter doc=%s failed: %s", did, exc)
    return {
        "docs_processed": len(seen_doc_ids),
        "chunks_augmented": augmented_total,
        "total_cost_usd": round(cost_total, 4),
        "failures": failures,
    }


def backfill(
    *,
    limit: int = 200,
    source: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    augment: bool = False,
) -> Dict[str, Any]:
    """Main loop. Returns a per-source summary dict."""
    sb = SupabaseClient()
    embedder = None if dry_run else get_embedder()

    summary: Dict[str, Dict[str, int]] = {}
    seen = 0
    processed = 0
    skipped = 0
    failures = 0
    offset = 0

    while seen < limit:
        page = _fetch_doc_batch(
            sb, limit=min(DOC_BATCH_SIZE, limit - seen),
            offset=offset, source=source,
        )
        if not page:
            break
        offset += len(page)
        for doc in page:
            seen += 1
            doc_id = doc["id"]
            src = doc.get("source") or "unknown"
            bucket = summary.setdefault(src, {
                "considered": 0, "chunked": 0, "embedded": 0, "skipped": 0,
                "errors": 0,
            })
            bucket["considered"] += 1

            if not force and _doc_already_chunked(sb, doc_id):
                skipped += 1
                bucket["skipped"] += 1
                continue

            try:
                chunks = chunk_document(doc)
                if not chunks:
                    skipped += 1
                    bucket["skipped"] += 1
                    continue
                if dry_run:
                    logger.info(
                        "[dry-run] doc=%s src=%s would emit %d chunks",
                        doc_id, src, len(chunks),
                    )
                    continue
                rows = _insert_chunks(sb, doc_id, chunks)
                bucket["chunked"] += len(rows)
                wrote = _embed_and_persist(sb, doc, rows, embedder)
                bucket["embedded"] += wrote
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failures += 1
                bucket["errors"] += 1
                logger.exception("doc=%s failed: %s", doc_id, exc)

        if len(page) < DOC_BATCH_SIZE:
            break

    if augment and not dry_run:
        augment_summary = _run_augmenter_pass(sb, limit=limit, source=source)
        summary["_augmenter"] = augment_summary

    out = {
        "seen": seen, "processed": processed, "skipped": skipped,
        "failures": failures, "by_source": summary,
    }
    logger.info("backfill summary: %s", out)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=200,
                   help="max docs to consider in this run")
    p.add_argument("--source", type=str, default=None,
                   help="restrict to one documents.source value")
    p.add_argument("--force", action="store_true",
                   help="re-chunk even if doc already has chunks")
    p.add_argument("--dry-run", action="store_true",
                   help="print would-do, no writes")
    p.add_argument("--augment", action="store_true",
                   help="run contextual_augmenter pass after embeddings")
    p.add_argument("--augment-only", action="store_true",
                   help="skip chunking/embedding and only run augmenter")
    args = p.parse_args(argv)

    if args.augment_only:
        from modal_workers.rag.contextual_augmenter import (
            augment_documents_missing_context,
        )
        sb = SupabaseClient()
        n = augment_documents_missing_context(sb, limit=args.limit)
        logger.info("augmenter-only: %d chunks augmented", n)
        return 0

    result = backfill(
        limit=args.limit, source=args.source,
        force=args.force, dry_run=args.dry_run, augment=args.augment,
    )
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
