"""Federal Register v3 ingestion path.

Searches Federal Register for FDA documents (AdComm notices, final rules, staff
reviews), fetches each document's raw text body, and writes through
document_writer to the documents table.

Replaces the v2 pattern where the regulatory specialist agent called
FederalRegisterClient.search() / get_document() and stuffed metadata into a
signal's raw_payload. In v3 the document is the unit of ingestion; orchestrator
synthesizes from the documents table downstream.

Run modes:
  - keyword search (e.g. drug name, sponsor name) — for backfilling around a
    known asset
  - daily sweep — pull all FDA documents published in the last N days; the
    asset linker (Sonnet pass-1 → pass-2) attaches them to fda_assets

Cadence (Phase 1): keyword search on-demand from the asset_documents linker;
daily sweep at 06:00 UTC via Modal scheduled function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional

from modal_workers.providers.federal_register import (
    FederalRegisterClient,
    FederalRegisterError,
)
from modal_workers.shared.document_writer import DocumentWriter, WriteResult

logger = logging.getLogger(__name__)

# FDA agency slugs the Federal Register API recognizes. The primary one is
# food-and-drug-administration; the *-hhs variant is occasionally used for
# combined HHS/FDA notices.
FDA_AGENCY_SLUGS = ["food-and-drug-administration"]

# Document types worth ingesting. NOTICE covers AdComm announcements, drug
# approvals, public meetings. RULE / PRORULE are final/proposed rules
# (e.g. labeling changes, REMS modifications). PRESDOCU is presidential docs,
# rarely FDA-relevant.
INGEST_DOC_TYPES = ["NOTICE", "RULE", "PRORULE"]

# Daily-sweep window: how far back to look on each invocation. 7 days covers
# weekend gaps and gives the asset linker time to attach docs without missing.
DEFAULT_SWEEP_DAYS = 7

# Per-search pagination. Federal Register caps per_page at 1000 but practically
# we want smaller pages for incremental progress.
DEFAULT_PER_PAGE = 100


@dataclass
class IngestRunResult:
    documents_seen: int = 0
    documents_written: int = 0
    documents_dedup_hit: int = 0
    documents_skipped_no_text: int = 0
    errors: int = 0
    written_ids: List[str] = field(default_factory=list)


def ingest_keyword_search(
    query: str,
    *,
    since: Optional[date] = None,
    until: Optional[date] = None,
    max_pages: int = 5,
    per_page: int = DEFAULT_PER_PAGE,
    client: Optional[FederalRegisterClient] = None,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Search the Federal Register for `query` over [since, until] and ingest
    every matching FDA document.

    Used by the asset linker to backfill documents on a newly-tracked asset
    (e.g. when a sponsor announces a new drug program, ingest historical FDA
    notices on the molecule)."""
    fr = client or FederalRegisterClient()
    dw = writer or DocumentWriter()
    result = IngestRunResult()

    for page in range(1, max_pages + 1):
        try:
            hits = fr.search(
                query,
                since=since,
                until=until,
                agencies=FDA_AGENCY_SLUGS,
                document_types=INGEST_DOC_TYPES,
                per_page=per_page,
                page=page,
            )
        except FederalRegisterError as exc:
            logger.error("federal_register search failed page=%d: %s", page, exc)
            result.errors += 1
            break

        if not hits:
            logger.info(
                "federal_register: query=%r since=%s page=%d returned no hits; stopping",
                query, since, page)
            break

        result.documents_seen += len(hits)
        for h in hits:
            outcome = _ingest_one_metadata(h, fr, dw)
            _accumulate(result, outcome)

        if len(hits) < per_page:
            # Last page (partial); no need to try the next.
            break

    logger.info(
        "federal_register ingest summary query=%r seen=%d wrote=%d dedup=%d "
        "no_text=%d errors=%d",
        query, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped_no_text, result.errors)
    return result


def ingest_daily_sweep(
    *,
    sweep_days: int = DEFAULT_SWEEP_DAYS,
    max_pages: int = 10,
    per_page: int = DEFAULT_PER_PAGE,
    client: Optional[FederalRegisterClient] = None,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull all FDA documents published in the last `sweep_days`. Run by the
    Modal scheduled function at 06:00 UTC (Phase 1 wiring)."""
    today = date.today()
    since = today - timedelta(days=sweep_days)

    fr = client or FederalRegisterClient()
    dw = writer or DocumentWriter()
    result = IngestRunResult()

    for page in range(1, max_pages + 1):
        try:
            # Empty `query` paired with agency filter pulls all FDA docs in window.
            hits = fr.search(
                "",  # no keyword filter
                since=since,
                until=today,
                agencies=FDA_AGENCY_SLUGS,
                document_types=INGEST_DOC_TYPES,
                per_page=per_page,
                page=page,
            )
        except FederalRegisterError as exc:
            logger.error("federal_register sweep failed page=%d: %s", page, exc)
            result.errors += 1
            break

        if not hits:
            break

        result.documents_seen += len(hits)
        for h in hits:
            outcome = _ingest_one_metadata(h, fr, dw)
            _accumulate(result, outcome)

        if len(hits) < per_page:
            break

    logger.info(
        "federal_register daily sweep since=%s seen=%d wrote=%d dedup=%d "
        "no_text=%d errors=%d",
        since, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped_no_text, result.errors)
    return result


@dataclass
class _IngestOutcome:
    written: bool = False
    dedup_hit: bool = False
    skipped_no_text: bool = False
    error: bool = False
    document_id: Optional[str] = None


def _ingest_one_metadata(
    metadata: dict,
    client: FederalRegisterClient,
    writer: DocumentWriter,
) -> _IngestOutcome:
    """Given normalized metadata from search(), fetch the raw text and write."""
    document_number = metadata.get("document_number")
    if not document_number:
        return _IngestOutcome(error=True)

    full = client.fetch_full_text(document_number)
    if not full or not full.get("raw_text"):
        return _IngestOutcome(skipped_no_text=True)

    raw_text: str = full["raw_text"]
    pub_date_str = full.get("publication_date")
    if not pub_date_str:
        logger.warning("federal_register: doc %s missing publication_date; skipping",
                       document_number)
        return _IngestOutcome(error=True)

    # Federal Register publication_date is a date string YYYY-MM-DD. Convert to
    # tz-aware datetime at midnight UTC for storage.
    try:
        published_at = datetime.fromisoformat(pub_date_str).replace(
            tzinfo=timezone.utc)
    except ValueError:
        logger.warning("federal_register: doc %s has unparseable date %s",
                       document_number, pub_date_str)
        return _IngestOutcome(error=True)

    try:
        result: WriteResult = writer.write_document(
            source="federal_register",
            source_doc_id=document_number,
            doc_type=full.get("type") or "NOTICE",
            raw_text=raw_text,
            published_at=published_at,
            url=full.get("html_url"),
            title=full.get("title"),
            is_pdf=False,
            extensions={
                "agency_names": full.get("agency_names") or [],
                "topics": full.get("topics") or [],
                "abstract": full.get("abstract"),
                "pdf_url": full.get("pdf_url"),
                "raw_text_url": full.get("raw_text_url"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("federal_register: write failed for %s: %s",
                         document_number, exc)
        return _IngestOutcome(error=True)

    return _IngestOutcome(
        written=result.was_new,
        dedup_hit=not result.was_new,
        document_id=result.document_id,
    )


def _accumulate(result: IngestRunResult, outcome: _IngestOutcome) -> None:
    if outcome.error:
        result.errors += 1
    if outcome.skipped_no_text:
        result.documents_skipped_no_text += 1
    if outcome.written:
        result.documents_written += 1
        if outcome.document_id:
            result.written_ids.append(outcome.document_id)
    if outcome.dedup_hit:
        result.documents_dedup_hit += 1
