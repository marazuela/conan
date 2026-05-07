"""backfill_anthropic_files — Stream 3.2 one-shot.

Walks documents where is_pdf=true AND anthropic_file_id IS NULL, uploads each
to Anthropic's Files API, and stores the returned file_id back on the row.

Usage:

    python -m modal_workers.scripts.backfill_anthropic_files [--limit N] [--dry-run]

Operator-triggered. Idempotent — never re-uploads rows that already have an id.
Failures are logged per-row and the script continues; a final summary lists how
many succeeded vs failed.

Reads PDF bytes from:
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
    DocumentWriter,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BATCH_SIZE = 50


def _fetch_batch(
    client: SupabaseClient, *, batch_size: int, offset: int = 0
) -> List[Dict[str, Any]]:
    return client._rest(
        "GET", "documents",
        params={
            "select": "id,storage_path,raw_text,title,source,source_doc_id,is_pdf,anthropic_file_id",
            "is_pdf": "eq.true",
            "anthropic_file_id": "is.null",
            "limit": str(batch_size),
            "offset": str(offset),
            "order": "fetched_at.asc",
        },
    ) or []


def _read_pdf_bytes(client: SupabaseClient, row: Dict[str, Any]) -> Optional[bytes]:
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
    offset = 0

    while True:
        if limit is not None and processed >= limit:
            break
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - processed)
        batch = _fetch_batch(sb, batch_size=size, offset=offset)
        if not batch:
            break

        for row in batch:
            processed += 1
            doc_id = row["id"]
            body = _read_pdf_bytes(sb, row)
            if not body:
                logger.info("doc=%s no body found (storage_path/raw_text both empty); skipping", doc_id)
                continue

            filename = row.get("title") or f"{row.get('source')}_{row.get('source_doc_id')}.pdf"
            if dry_run:
                logger.info("[dry-run] would upload doc=%s filename=%s bytes=%d",
                            doc_id, filename, len(body))
                continue

            file_id = writer._upload_to_anthropic(body, filename)
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

    summary = {"processed": processed, "uploaded": uploaded, "failed": failed}
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
