"""backfill_anthropic_files — drains documents that need a Files-API file_id.

Walks documents where anthropic_file_id IS NULL AND raw_text bytes are large
enough to justify a Files API upload (size gate matches document_writer's
MIN_UPLOAD_BYTES — see L42), uploads each to Anthropic's Files API, and stores
the returned file_id back on the row.

Body MIME type is decided by the documents.is_pdf column. Native Citations
works for both PDF and text/plain payloads, so the size gate is the operative
selector, not is_pdf.

Usage:

    python -m modal_workers.scripts.backfill_anthropic_files [--limit N] [--dry-run]

Idempotent — never re-uploads rows that already have an id. Safe to schedule
periodically (see modal_workers/scanners/anthropic_files_backfill_scheduler.py)
to drain whatever the at-ingest upload path missed (e.g. ingesters predating
this wiring, or transient API failures).

Failures are logged per-row and the script continues; a final summary lists how
many succeeded vs failed.

Reads bytes from:
  1. documents.storage_path → Supabase Storage (preferred for large files)
  2. documents.raw_text → inline bytes (encoded utf-8 — see document_writer.py
     L120-L122 for the encoding contract)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from modal_workers.shared.document_writer import (
    DOCUMENT_STORAGE_BUCKET,
    MIN_UPLOAD_BYTES,
    DocumentWriter,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BATCH_SIZE = 50


def _fetch_batch(
    client: SupabaseClient, *, batch_size: int, offset: int = 0
) -> List[Dict[str, Any]]:
    # Filter is "no file_id yet". The size gate is applied per-row inside the
    # body-fetch step (we can't easily express "byte length of raw_text OR size
    # in storage" in PostgREST). The is_pdf column is read but no longer used
    # as a selector — it's the MIME hint, not the eligibility predicate.
    return client._rest(
        "GET", "documents",
        params={
            "select": "id,storage_path,raw_text,title,source,source_doc_id,is_pdf,anthropic_file_id",
            "anthropic_file_id": "is.null",
            "limit": str(batch_size),
            "offset": str(offset),
            "order": "fetched_at.asc",
        },
    ) or []


def _read_doc_bytes(client: SupabaseClient, row: Dict[str, Any]) -> Optional[bytes]:
    """Returns the document body bytes. Used to both apply the size gate and
    feed the Files API. PDFs come from storage; text payloads come from either
    storage_path or the inline raw_text column (utf-8 encoded)."""
    storage_path = row.get("storage_path")
    if storage_path:
        try:
            return client.read_cache(DOCUMENT_STORAGE_BUCKET, storage_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("doc=%s storage read failed: %s", row["id"], exc)
            return None
    raw_text = row.get("raw_text")
    if raw_text:
        return raw_text.encode("utf-8")
    return None


def run(*, limit: Optional[int] = None, dry_run: bool = False) -> Dict[str, int]:
    sb = SupabaseClient()
    writer = DocumentWriter(client=sb)
    if not writer.anthropic_api_key:
        logger.error(
            "ANTHROPIC_ORCHESTRATOR_KEY not set — refusing to backfill without an API key.",
        )
        return {"processed": 0, "uploaded": 0, "failed": 0, "skipped_no_key": 1}

    processed = 0
    uploaded = 0
    failed = 0
    skipped_too_small = 0
    skipped_no_body = 0
    offset = 0

    while True:
        if limit is not None and processed >= limit:
            break
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - processed)
        batch = _fetch_batch(sb, batch_size=size, offset=offset)
        if not batch:
            break

        # Track how many rows in this batch we advanced past in the cursor —
        # rows skipped by the size gate or missing-body check are NOT uploaded
        # but still consume the offset, otherwise we loop forever on them.
        for row in batch:
            processed += 1
            doc_id = row["id"]
            body = _read_doc_bytes(sb, row)
            if not body:
                skipped_no_body += 1
                logger.info(
                    "doc=%s no body found (storage_path/raw_text both empty); skipping",
                    doc_id,
                )
                continue

            # Size gate — must match document_writer's MIN_UPLOAD_BYTES so the
            # at-ingest path and the backfill path agree on eligibility.
            if len(body) < MIN_UPLOAD_BYTES:
                skipped_too_small += 1
                logger.debug(
                    "doc=%s below size gate (bytes=%d < %d); skipping",
                    doc_id, len(body), MIN_UPLOAD_BYTES,
                )
                continue

            is_pdf = bool(row.get("is_pdf"))
            ext = "pdf" if is_pdf else "txt"
            filename = (
                row.get("title")
                or f"{row.get('source')}_{row.get('source_doc_id')}.{ext}"
            )
            if dry_run:
                logger.info(
                    "[dry-run] would upload doc=%s filename=%s mime=%s bytes=%d",
                    doc_id, filename,
                    "application/pdf" if is_pdf else "text/plain",
                    len(body),
                )
                continue

            file_id = writer._upload_to_anthropic(body, filename, is_pdf=is_pdf)
            if not file_id:
                failed += 1
                continue

            try:
                sb._rest(
                    "PATCH", "documents",
                    params={"id": f"eq.{doc_id}"},
                    json_body={"anthropic_file_id": file_id},
                )
                uploaded += 1
                logger.info("doc=%s uploaded → file_id=%s", doc_id, file_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("doc=%s upload OK but DB patch failed: %s", doc_id, exc)

        offset += len(batch)
        # When we fetched < BATCH_SIZE we're at the end.
        if len(batch) < size:
            break

    summary = {
        "processed": processed,
        "uploaded": uploaded,
        "failed": failed,
        "skipped_too_small": skipped_too_small,
        "skipped_no_body": skipped_no_body,
    }
    logger.info("backfill complete: %s", summary)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max documents to process. Default: all eligible.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log intended uploads without calling the Files API.")
    args = parser.parse_args(argv)
    summary = run(limit=args.limit, dry_run=args.dry_run)
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
