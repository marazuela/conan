"""EDGAR v3 ingestion path.

Discovers SEC filings via EFTS (search-index), fetches each filing's primary-
document text, and writes through document_writer to the documents table.

Relies on the existing modal_workers.shared.edgar_efts client (efts_search +
fetch_filing_text) which already handles rate limiting (9 req/s SEC ceiling),
retries on 429/5xx, and HTML-to-text cleanup.

Run modes:
  - keyword search (any form type, any date range) — used by the asset linker
    to backfill or hunt specific filings (e.g. drug name in 8-K Item 8.01)
  - form sweep — daily harvest of new 8-K / 10-K / 10-Q / S-1 / 13D / 13G /
    Form 4 filings published in the last N days
  - by-CIK sweep — pull recent filings for a specific issuer (used when a new
    sponsor enters the watchlist)

EDGAR-specific notes:
  - source_doc_id encodes (cik, adsh) — the canonical filing accession-shaped
    identifier. Hash dedup (source, source_content_hash) catches re-fetches
    of the same filing where a normalization tweak changes spacing.
  - doc_type is the SEC form type ("8-K", "10-K", "10-Q", "S-1", "SC 13D", etc.).
  - Form 4 (insider transactions) is XML, not HTML; fetch_filing_text strips
    tags but the result is a flattened string suitable for orchestrator reads.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional

from modal_workers.shared.edgar_efts import efts_search, fetch_filing_text
from modal_workers.shared.document_writer import DocumentWriter, WriteResult

logger = logging.getLogger(__name__)

# Forms that the v3 orchestrator cares about for FDA-vertical synthesis.
# 8-K: real-time material events (PDUFA delays, FDA correspondence, AdComm
#   results, clinical readouts).
# 10-K / 10-Q: pipeline tables, risk factors, FDA correspondence references.
# S-1 / 424B: IPO biotech principal product candidate tables.
# SC 13D / SC 13G: activist + concentrated ownership disclosures.
# Form 4: insider transactions; written as one document per Form 4 (per plan).
# 6-K: foreign-private-issuer equivalent of 8-K (used for foreign biotechs).
DEFAULT_FDA_FORM_FILTER = (
    "8-K,10-K,10-Q,S-1,424B1,424B2,424B3,424B4,424B5,424B7,SC 13D,SC 13G,SC 13D/A,"
    "SC 13G/A,4,4/A,6-K"
)

DEFAULT_SWEEP_DAYS = 7
DEFAULT_PER_PAGE = 50  # EFTS sweet spot; max is 100


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
    forms: str = DEFAULT_FDA_FORM_FILTER,
    user_agent: Optional[str] = None,
    size: int = DEFAULT_PER_PAGE,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Search EFTS for `query` over [since, until] and ingest matching filings.

    Used by the asset linker to backfill / hunt specific FDA-relevant filings
    on a tracked asset (e.g. drug name in 8-K Item 8.01)."""
    today = date.today()
    since = since or (today - timedelta(days=DEFAULT_SWEEP_DAYS))
    until = until or today
    ua = user_agent or _resolve_user_agent()
    dw = writer or DocumentWriter()

    result = IngestRunResult()
    hits = efts_search(
        query, since.isoformat(), until.isoformat(),
        forms=forms, size=size, user_agent=ua,
    )
    if not hits:
        logger.info(
            "edgar ingest: query=%r forms=%s window=%s..%s returned no hits",
            query, forms, since, until)
        return result

    result.documents_seen = len(hits)
    try:
        for h in hits:
            try:
                outcome = _ingest_one_hit(h, dw, ua)
            except Exception as exc:  # noqa: BLE001 — D-110a: defense in depth
                # _ingest_one_hit already catches its own exceptions and returns
                # _IngestOutcome(error=True). This outer guard exists for the
                # truly unexpected (programming errors, OOM, _accumulate bugs)
                # so a single bad hit never aborts the whole batch.
                logger.exception(
                    "edgar ingest: unexpected exception on hit %r: %s",
                    (h or {}).get("_id"), exc)
                outcome = _IngestOutcome(error=True)
            _accumulate(result, outcome)
    finally:
        # D-110a: emit summary even on abnormal exit so partial work is visible.
        logger.info(
            "edgar ingest summary query=%r seen=%d wrote=%d dedup=%d "
            "no_text=%d errors=%d",
            query, result.documents_seen, result.documents_written,
            result.documents_dedup_hit, result.documents_skipped_no_text, result.errors)
    return result


def ingest_form_sweep(
    *,
    forms: str = DEFAULT_FDA_FORM_FILTER,
    sweep_days: int = DEFAULT_SWEEP_DAYS,
    user_agent: Optional[str] = None,
    size_per_query: int = 100,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Daily harvest of new filings of `forms` published in the last `sweep_days`.

    Run by a Modal scheduled function (Phase 1 wiring)."""
    today = date.today()
    since = today - timedelta(days=sweep_days)
    ua = user_agent or _resolve_user_agent()
    dw = writer or DocumentWriter()

    result = IngestRunResult()
    # Empty query + form filter pulls all filings of those forms.
    hits = efts_search(
        "", since.isoformat(), today.isoformat(),
        forms=forms, size=size_per_query, user_agent=ua,
    )
    if not hits:
        logger.info("edgar sweep: forms=%s since=%s returned no hits", forms, since)
        return result

    result.documents_seen = len(hits)
    try:
        for h in hits:
            try:
                outcome = _ingest_one_hit(h, dw, ua)
            except Exception as exc:  # noqa: BLE001 — D-110a: defense in depth
                logger.exception(
                    "edgar sweep: unexpected exception on hit %r: %s",
                    (h or {}).get("_id"), exc)
                outcome = _IngestOutcome(error=True)
            _accumulate(result, outcome)
    finally:
        logger.info(
            "edgar form sweep forms=%s since=%s seen=%d wrote=%d dedup=%d "
            "no_text=%d errors=%d",
            forms, since, result.documents_seen, result.documents_written,
            result.documents_dedup_hit, result.documents_skipped_no_text, result.errors)
    return result


def ingest_by_cik(
    cik: str,
    *,
    since: Optional[date] = None,
    until: Optional[date] = None,
    forms: str = DEFAULT_FDA_FORM_FILTER,
    user_agent: Optional[str] = None,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull recent filings for a specific issuer (CIK). Used when a new sponsor
    is added to the watchlist and we want to backfill its filing history."""
    today = date.today()
    since = since or (today - timedelta(days=180))  # 6mo default
    until = until or today
    ua = user_agent or _resolve_user_agent()
    dw = writer or DocumentWriter()

    cik_clean = cik.lstrip("0") or "0"
    # EFTS supports a `ciks` parameter, but the shared efts_search wrapper takes
    # a plain query. We pass the CIK as part of the q= filter via EFTS's
    # implicit cik field syntax; if the wrapper grows a `ciks` kwarg later, we
    # switch to that.
    query = f"cik:{cik_clean}"

    return ingest_keyword_search(
        query, since=since, until=until, forms=forms, user_agent=ua,
        size=100, writer=dw,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

@dataclass
class _IngestOutcome:
    written: bool = False
    dedup_hit: bool = False
    skipped_no_text: bool = False
    error: bool = False
    document_id: Optional[str] = None


def _ingest_one_hit(hit: dict, writer: DocumentWriter, user_agent: str) -> _IngestOutcome:
    """Given an EFTS hit, fetch the filing body and write a documents row."""
    file_id = hit.get("_id")  # EFTS hit id, format "<adsh>:<filename>"
    src = hit.get("_source") or {}
    if not file_id or not src:
        return _IngestOutcome(error=True)

    # Required fields from _source
    ciks = src.get("ciks") or []
    cik = ciks[0] if ciks else None
    adsh = src.get("adsh")
    form = src.get("form") or src.get("file_type") or "UNKNOWN"
    file_date_str = src.get("file_date")
    if not (cik and adsh and file_date_str):
        return _IngestOutcome(error=True)

    raw_text = fetch_filing_text(file_id, cik, adsh, user_agent=user_agent)
    if not raw_text:
        return _IngestOutcome(skipped_no_text=True)

    try:
        published_at = datetime.fromisoformat(file_date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("edgar ingest: hit %s has unparseable file_date %s",
                       file_id, file_date_str)
        return _IngestOutcome(error=True)

    # Build a stable source_doc_id: cik + adsh uniquely identifies the filing.
    source_doc_id = f"{cik}:{adsh}"
    title = (src.get("display_names") or [None])[0] or src.get("name")
    extensions = {
        "ciks": ciks,
        "tickers": src.get("tickers") or [],
        "display_names": src.get("display_names") or [],
        "adsh": adsh,
        "file_id": file_id,
        "form": form,
        "file_type": src.get("file_type"),
        "items": src.get("items") or [],
    }

    archives_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0') or '0'}/"
        f"{adsh.replace('-', '')}/"
    )

    try:
        result: WriteResult = writer.write_document(
            source="edgar",
            source_doc_id=source_doc_id,
            doc_type=form,
            raw_text=raw_text,
            published_at=published_at,
            url=archives_url,
            title=title,
            is_pdf=False,
            upload_to_anthropic=True,  # size-gated in document_writer (MIN_UPLOAD_BYTES)
            extensions=extensions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("edgar ingest: write failed for %s: %s", source_doc_id, exc)
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


def _resolve_user_agent() -> str:
    """SEC requires a contact User-Agent. Mirrors the convention used by
    edgar_filing_monitor and insider_form4_scanner."""
    return os.environ.get(
        "SEC_USER_AGENT",
        "Conan/1.0 (FDA orchestrator; https://github.com/marazuela/conan)",
    )
