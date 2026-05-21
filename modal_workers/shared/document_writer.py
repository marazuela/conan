"""
document_writer — canonical writer for the v3 documents table.

Every ingestion adapter (Federal Register, EDGAR, openFDA, ClinicalTrials, DailyMed,
FAERS, AdComm transcripts, warning letters, 483s, PubMed, bioRxiv, Polygon news,
press releases) writes here via DocumentWriter.write_document(). This is the single
ingestion path; scanners no longer emit signals.

The flow:
  1. Compute sha256 of raw_text (caller can pre-compute and pass).
  2. Dedupe via UNIQUE (source, source_content_hash). On conflict, return existing id.
  3. If raw_text > 512KB, upload bytes to Supabase Storage and store storage_path
     (raw_text column left NULL to keep row size manageable).
  4. If is_pdf and the orchestrator API key is configured, upload to Anthropic
     Files API and store anthropic_file_id (deferred; stub for Phase 5).
  5. INSERT into documents table; PostgREST returns the row id.
  6. (Future) Postgres trigger fires NOTIFY document_inserted, doc_id — subscribed
     by extractor + asset linker Modal workers. For now, callers can poll or
     downstream Modal functions can listen via Supabase Realtime once the docs
     table is added to the realtime publication.

Idempotency: writing the same (source, source_doc_id, raw_text) twice returns the
same doc_id without re-uploading to Storage / Files API.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# Inline storage threshold: rows with raw_text under this size keep raw_text inline
# in the documents row. Larger payloads go to Supabase Storage and storage_path
# is set instead. Postgres TOAST handles up to ~1MB efficiently but row scans get
# slow above that. 512KB is a comfortable cap.
INLINE_RAW_TEXT_BYTES = 512 * 1024

# Storage bucket name (must already exist; created via Supabase CLI or dashboard).
DOCUMENT_STORAGE_BUCKET = "documents"

# Anthropic Files API endpoint (used by _maybe_upload_to_anthropic).
ANTHROPIC_FILES_ENDPOINT = "https://api.anthropic.com/v1/files"
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._() -]+")
_FILENAME_SPACE_RE = re.compile(r"\s+")
_MAX_ANTHROPIC_FILENAME_CHARS = 255

# Source values must match the CHECK constraint in 20260506000010_v3_phase_0_1_schema.sql.
VALID_SOURCES = {
    "edgar", "federal_register", "openfda", "clinicaltrials", "dailymed",
    "faers", "fda_advisory", "fda_warning_letter", "fda_483",
    "pubmed", "biorxiv", "medrxiv", "polygon_news", "press_release",
}


@dataclass
class WriteResult:
    document_id: str
    was_new: bool                   # True if INSERT happened, False if conflict-skip
    storage_path: Optional[str]     # Set if raw_text was offloaded to Storage
    anthropic_file_id: Optional[str]


def compute_content_hash(raw_text: str) -> str:
    """sha256 of raw_text (utf-8). Caller can pre-compute or let writer do it."""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def safe_anthropic_filename(filename: str) -> str:
    """Return a Files API-safe PDF filename.

    Anthropic rejects path separators, control-ish punctuation, and names over
    255 chars. Source titles often contain SEC suffixes like `/DE/` or full
    Federal Register titles, so sanitize at the upload boundary.
    """
    candidate = str(filename or "").replace("/", " ").replace("\\", " ")
    candidate = _FILENAME_SAFE_RE.sub(" ", candidate)
    candidate = _FILENAME_SPACE_RE.sub(" ", candidate).strip(" ._-")
    if not candidate:
        candidate = "document"
    if not candidate.lower().endswith(".pdf"):
        candidate = f"{candidate}.pdf"
    if len(candidate) <= _MAX_ANTHROPIC_FILENAME_CHARS:
        return candidate
    stem, ext = os.path.splitext(candidate)
    ext = ext or ".pdf"
    return f"{stem[:_MAX_ANTHROPIC_FILENAME_CHARS - len(ext)].rstrip(' ._-')}{ext}"


class DocumentWriter:
    """Wraps SupabaseClient with the v3 ingestion contract."""

    def __init__(self, client: Optional[SupabaseClient] = None,
                 anthropic_api_key: Optional[str] = None):
        self.client = client or SupabaseClient()
        # If set, PDFs are uploaded to Anthropic Files API and the file_id stored
        # in documents.anthropic_file_id. If None, we skip the upload (orchestrator
        # will upload lazily on first use). Phase 5 wiring.
        self.anthropic_api_key = anthropic_api_key or os.environ.get(
            "ANTHROPIC_ORCHESTRATOR_KEY")

    def write_document(
        self,
        *,
        source: str,
        source_doc_id: str,
        doc_type: str,
        raw_text: str,
        published_at: datetime,
        url: Optional[str] = None,
        title: Optional[str] = None,
        is_pdf: bool = False,
        language: str = "en",
        extensions: Optional[Dict[str, Any]] = None,
        upload_to_anthropic: bool = False,
        precomputed_hash: Optional[str] = None,
    ) -> WriteResult:
        """Write one document. Idempotent on (source, source_content_hash)."""
        if source not in VALID_SOURCES:
            raise ValueError(
                f"Invalid source {source!r}; must be one of {sorted(VALID_SOURCES)}")

        if not raw_text:
            raise ValueError("raw_text must be non-empty")

        content_hash = precomputed_hash or compute_content_hash(raw_text)
        raw_text_tokens = _approximate_tokens(raw_text)
        raw_text_bytes = len(raw_text.encode("utf-8"))

        # Storage offload for large bodies.
        storage_path: Optional[str] = None
        inline_text: Optional[str] = raw_text
        if raw_text_bytes > INLINE_RAW_TEXT_BYTES:
            storage_path = _build_storage_path(source, content_hash)
            self.client.write_cache(
                DOCUMENT_STORAGE_BUCKET,
                storage_path,
                raw_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8" if not is_pdf else "application/pdf",
            )
            inline_text = None  # don't duplicate in Postgres row

        # Anthropic Files API upload — only if explicitly requested AND key configured.
        # Most callers leave this False; orchestrator does the upload lazily on first use.
        anthropic_file_id: Optional[str] = None
        if upload_to_anthropic and self.anthropic_api_key and is_pdf:
            anthropic_file_id = self._upload_to_anthropic(
                raw_text.encode("utf-8"), title or f"{source}_{source_doc_id}.pdf")

        # Insert via PostgREST. Conflict on (source, source_content_hash) returns the
        # existing row's id.
        row = {
            "source": source,
            "source_doc_id": source_doc_id,
            "source_content_hash": content_hash,
            "url": url,
            "doc_type": doc_type,
            "storage_path": storage_path,
            "raw_text": inline_text,
            "raw_text_tokens": raw_text_tokens,
            "anthropic_file_id": anthropic_file_id,
            "is_pdf": is_pdf,
            "title": title,
            "published_at": published_at.astimezone(timezone.utc).isoformat(),
            "language": language,
            "extensions": extensions or {},
        }

        # PostgREST upsert with merge-duplicates: returns the row whether new or existing.
        # We can detect new vs existing by comparing fetched_at to current time.
        rows = self.client._rest(
            "POST", "documents",
            params={"on_conflict": "source,source_content_hash"},
            json_body=row,
            prefer="return=representation,resolution=merge-duplicates",
        )
        if not rows:
            # PostgREST returned no rows — should not happen with merge-duplicates,
            # but defend.
            raise RuntimeError(
                f"PostgREST returned no row for documents insert "
                f"(source={source}, hash={content_hash[:12]}…)")

        record = rows[0]
        was_new = self._is_freshly_inserted(record)

        if was_new:
            logger.info(
                "document_writer: wrote source=%s doc_type=%s id=%s tokens=%d storage=%s",
                source, doc_type, record["id"], raw_text_tokens,
                storage_path or "inline")
        else:
            logger.info(
                "document_writer: dedupe-hit source=%s id=%s",
                source, record["id"])

        return WriteResult(
            document_id=record["id"],
            was_new=was_new,
            storage_path=record.get("storage_path"),
            anthropic_file_id=record.get("anthropic_file_id"),
        )

    def _upload_to_anthropic(self, body: bytes, filename: str) -> Optional[str]:
        """Upload bytes to Anthropic Files API. Returns file_id or None on failure.

        Stream 3.2: real implementation. Uses the anthropic SDK Beta files API
        (the GA endpoint accepts the same call shape — see Anthropic Files docs).
        Failure returns None and logs; the caller falls back to raw_text.
        """
        try:
            import anthropic
        except ImportError:
            logger.warning(
                "document_writer: anthropic SDK not installed; "
                "Files API upload skipped for %s", filename,
            )
            return None

        try:
            client = anthropic.Anthropic(api_key=self.anthropic_api_key)
            # files.create accepts a tuple (filename, bytes, mime_type) under the
            # standard SDK; if the SDK exposes it under .beta.files we route there.
            files_api = getattr(getattr(client, "beta", None), "files", None) or getattr(
                client, "files", None
            )
            if files_api is None:
                logger.warning(
                    "document_writer: anthropic SDK has no files API surface; "
                    "skipping upload for %s", filename,
                )
                return None
            safe_filename = safe_anthropic_filename(filename)
            resp = files_api.create(
                file=(safe_filename, body, "application/pdf"),
            )
            file_id = getattr(resp, "id", None)
            if file_id:
                logger.info(
                    "document_writer: uploaded to anthropic files api filename=%s file_id=%s",
                    safe_filename, file_id,
                )
            return file_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "document_writer: anthropic Files API upload failed for %s: %s",
                filename, exc,
            )
            return None

    def _is_freshly_inserted(self, record: Dict[str, Any]) -> bool:
        """Heuristic: if fetched_at is within the last 5 seconds, this is a fresh row.
        Older rows came from a prior insert (PostgREST returned the existing row
        via merge-duplicates)."""
        fetched_at_str = record.get("fetched_at")
        if not fetched_at_str:
            return True  # assume new if no timestamp
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
        except ValueError:
            return True
        delta = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        return abs(delta) < 5.0


def _approximate_tokens(text: str) -> int:
    """Rough token count for budget math (Anthropic tokenizer ratio ~3.7 chars/token).
    Pre-counted at ingest so orchestrator can plan corpora without re-tokenizing."""
    return max(1, int(len(text) / 3.7))


def _build_storage_path(source: str, content_hash: str) -> str:
    """Storage path schema: <source>/<hash[:2]>/<hash>.txt — sharded by hash prefix
    for filesystem-friendly fan-out."""
    return f"{source}/{content_hash[:2]}/{content_hash}.txt"
