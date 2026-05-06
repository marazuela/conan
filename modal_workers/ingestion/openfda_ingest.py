"""openFDA v3 ingestion path.

openFDA (https://api.fda.gov/) exposes FDA's public drug datasets as a single
JSON API. For the orchestrator we ingest:

  - drug/drugsfda — NDA/BLA approval history (sponsor, drug, application_number,
    submission_type, status_date). This is the canonical "did the FDA approve
    drug X" record. One document = one application_number's submission record.

  - drug/event — FAERS adverse event reports. Volume is huge (~10M reports/year);
    we only ingest aggregated-by-week-by-product summaries to control budget.
    Treated as a separate corpus query later in Phase 4.

  - drug/label — DailyMed labels. One document per setid.

For the MVP we ingest drugsfda + label. event ingestion is deferred until the
orchestrator is wiring real assessments.

All openFDA endpoints are public, unauthenticated, and rate-limited at
240 req/min anonymous (40k req/day). We respect those via per-request
backoff; no shared rate limiter needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.document_writer import DocumentWriter, WriteResult

logger = logging.getLogger(__name__)

OPENFDA_BASE = "https://api.fda.gov"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_PAGE_LIMIT = 100  # openFDA caps at 1000 per request, but smaller pages
                          # play better with the rate limit + give incremental progress


class OpenFDAError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"openfda http {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class IngestRunResult:
    documents_seen: int = 0
    documents_written: int = 0
    documents_dedup_hit: int = 0
    documents_skipped: int = 0
    errors: int = 0
    written_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _openfda_get(path: str, params: Dict[str, Any], *,
                 attempts: int = 3, backoff_s: float = 0.5,
                 session: Optional[requests.Session] = None) -> Optional[Dict[str, Any]]:
    sess = session or requests.Session()
    url = f"{OPENFDA_BASE}/{path.lstrip('/')}"
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            r = sess.get(url, params=params, timeout=DEFAULT_TIMEOUT_S)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            raise
        if r.status_code == 404:
            # openFDA returns 404 when search yields no results.
            return None
        if r.status_code == 429 or r.status_code >= 500:
            last_exc = OpenFDAError(r.status_code, r.text)
            if attempt < attempts - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            raise last_exc
        if r.status_code >= 400:
            raise OpenFDAError(r.status_code, r.text)
        try:
            return r.json()
        except ValueError:
            return None
    if last_exc is not None:
        raise last_exc
    return None


# ---------------------------------------------------------------------------
# drug/drugsfda — NDA / BLA approvals
# ---------------------------------------------------------------------------

def ingest_drugsfda_approvals(
    *,
    since: Optional[date] = None,
    until: Optional[date] = None,
    sponsor_search: Optional[str] = None,
    application_search: Optional[str] = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = 5,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull NDA/BLA approval records from openFDA drug/drugsfda.

    Each application_number → one document. raw_text is a JSON-formatted summary
    of the application's submission history (Sonnet extractor handles structuring
    downstream). source_doc_id = application_number.
    """
    today = date.today()
    since = since or (today - timedelta(days=30))
    until = until or today

    search_clauses: List[str] = [
        f"submissions.submission_status_date:[{since.isoformat()} TO {until.isoformat()}]",
    ]
    if sponsor_search:
        search_clauses.append(f"sponsor_name:\"{sponsor_search}\"")
    if application_search:
        search_clauses.append(f"application_number:\"{application_search}\"")

    search = " AND ".join(search_clauses)
    dw = writer or DocumentWriter()
    result = IngestRunResult()
    skip = 0

    for page_idx in range(max_pages):
        body = _openfda_get(
            "/drug/drugsfda.json",
            params={"search": search, "limit": page_limit, "skip": skip},
        )
        if not body:
            break
        results = body.get("results") or []
        if not results:
            break

        result.documents_seen += len(results)
        for app in results:
            outcome = _ingest_drugsfda_record(app, dw)
            _accumulate(result, outcome)

        if len(results) < page_limit:
            break
        skip += page_limit

    logger.info(
        "openfda drugsfda summary search=%r seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d",
        search, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors)
    return result


def _ingest_drugsfda_record(app: Dict[str, Any], writer: DocumentWriter) -> "_IngestOutcome":
    application_number = app.get("application_number")
    if not application_number:
        return _IngestOutcome(error=True)

    submissions = app.get("submissions") or []
    if not submissions:
        return _IngestOutcome(skipped=True)

    # Pick the most recent submission as the "canonical" published_at.
    most_recent: Dict[str, Any] = {}
    most_recent_date: Optional[str] = None
    for s in submissions:
        d = s.get("submission_status_date")
        if d and (most_recent_date is None or d > most_recent_date):
            most_recent_date = d
            most_recent = s

    if not most_recent_date:
        return _IngestOutcome(skipped=True)

    try:
        published_at = datetime.strptime(most_recent_date, "%Y%m%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return _IngestOutcome(error=True)

    raw_text = _format_drugsfda_record_as_text(app, most_recent)
    sponsor = app.get("sponsor_name")
    products = app.get("products") or []
    drug_names = [p.get("brand_name") for p in products if p.get("brand_name")]

    extensions = {
        "application_number": application_number,
        "sponsor_name": sponsor,
        "drug_names": drug_names,
        "submissions_count": len(submissions),
        "most_recent_submission_status": most_recent.get("submission_status"),
        "most_recent_submission_type": most_recent.get("submission_type"),
        "products": products,
    }

    try:
        result: WriteResult = writer.write_document(
            source="openfda",
            source_doc_id=application_number,
            doc_type="drugsfda_application",
            raw_text=raw_text,
            published_at=published_at,
            url=f"{OPENFDA_BASE}/drug/drugsfda.json?search=application_number:{application_number}",
            title=f"{sponsor or 'Unknown sponsor'} — {', '.join(drug_names) or application_number}",
            is_pdf=False,
            extensions=extensions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("openfda drugsfda: write failed for %s: %s",
                         application_number, exc)
        return _IngestOutcome(error=True)

    return _IngestOutcome(
        written=result.was_new,
        dedup_hit=not result.was_new,
        document_id=result.document_id,
    )


def _format_drugsfda_record_as_text(app: Dict[str, Any], most_recent: Dict[str, Any]) -> str:
    """Render the drugsfda record as a human-readable text block. Stable
    formatting so the orchestrator's content_hash dedupe is meaningful."""
    application_number = app.get("application_number", "?")
    sponsor = app.get("sponsor_name", "?")
    products = app.get("products") or []
    submissions = app.get("submissions") or []

    lines: List[str] = [
        f"FDA Application: {application_number}",
        f"Sponsor: {sponsor}",
        "",
        "Products:",
    ]
    for p in products:
        lines.append(
            f"  - {p.get('brand_name', '?')} "
            f"({p.get('active_ingredients', [{}])[0].get('name', '?') if p.get('active_ingredients') else '?'}) "
            f"— {p.get('dosage_form', '?')} {p.get('route', '?')} "
            f"strength={p.get('active_ingredients', [{}])[0].get('strength', '?') if p.get('active_ingredients') else '?'} "
            f"marketing_status={p.get('marketing_status', '?')}"
        )

    lines.append("")
    lines.append(f"Submission History ({len(submissions)} total, most recent first):")
    sorted_subs = sorted(submissions,
                         key=lambda s: s.get("submission_status_date", ""),
                         reverse=True)
    for s in sorted_subs[:20]:  # cap at 20 most recent
        lines.append(
            f"  {s.get('submission_status_date', '?')} "
            f"{s.get('submission_type', '?')} "
            f"{s.get('submission_number', '?')}: "
            f"{s.get('submission_status', '?')} "
            f"({s.get('submission_class_code', '?')})"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# drug/label — DailyMed structured product labels
# ---------------------------------------------------------------------------

def ingest_drug_label_recent(
    *,
    since: Optional[date] = None,
    until: Optional[date] = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = 10,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull recent drug-label changes (effective_time within window). Each
    setid (label revision) becomes one document.

    Source field is 'dailymed' (canonical home of structured product labels)
    to keep the source taxonomy aligned with the orchestrator's expectations."""
    today = date.today()
    since = since or (today - timedelta(days=30))
    until = until or today

    search = (f"effective_time:[{since.strftime('%Y%m%d')} "
              f"TO {until.strftime('%Y%m%d')}]")
    dw = writer or DocumentWriter()
    result = IngestRunResult()
    skip = 0

    for page_idx in range(max_pages):
        body = _openfda_get(
            "/drug/label.json",
            params={"search": search, "limit": page_limit, "skip": skip},
        )
        if not body:
            break
        labels = body.get("results") or []
        if not labels:
            break

        result.documents_seen += len(labels)
        for label in labels:
            outcome = _ingest_label_record(label, dw)
            _accumulate(result, outcome)

        if len(labels) < page_limit:
            break
        skip += page_limit

    logger.info(
        "openfda label summary since=%s seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d",
        since, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors)
    return result


def _ingest_label_record(label: Dict[str, Any], writer: DocumentWriter) -> "_IngestOutcome":
    set_id = label.get("set_id")
    if not set_id:
        return _IngestOutcome(error=True)
    effective_time = label.get("effective_time")
    if not effective_time:
        return _IngestOutcome(skipped=True)
    try:
        published_at = datetime.strptime(effective_time, "%Y%m%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return _IngestOutcome(error=True)

    raw_text = _format_label_as_text(label)
    openfda_meta = label.get("openfda") or {}
    extensions = {
        "set_id": set_id,
        "effective_time": effective_time,
        "version": label.get("version"),
        "openfda": {
            "application_number": openfda_meta.get("application_number") or [],
            "brand_name": openfda_meta.get("brand_name") or [],
            "generic_name": openfda_meta.get("generic_name") or [],
            "manufacturer_name": openfda_meta.get("manufacturer_name") or [],
            "product_type": openfda_meta.get("product_type") or [],
            "route": openfda_meta.get("route") or [],
        },
    }
    title_brand = (openfda_meta.get("brand_name") or [None])[0]
    title_generic = (openfda_meta.get("generic_name") or [None])[0]
    title = title_brand or title_generic or set_id

    try:
        result: WriteResult = writer.write_document(
            source="dailymed",
            source_doc_id=set_id,
            doc_type="drug_label",
            raw_text=raw_text,
            published_at=published_at,
            url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}",
            title=str(title),
            is_pdf=False,
            extensions=extensions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("openfda label: write failed for set_id=%s: %s", set_id, exc)
        return _IngestOutcome(error=True)

    return _IngestOutcome(
        written=result.was_new,
        dedup_hit=not result.was_new,
        document_id=result.document_id,
    )


def _format_label_as_text(label: Dict[str, Any]) -> str:
    """Flatten a DailyMed structured label into prose for the documents row.
    Sections shipped: indications, dosage, warnings (boxed_warning is the
    highest-signal field), adverse_reactions, drug_interactions,
    contraindications, mechanism_of_action."""
    sections = [
        ("BOXED WARNING", label.get("boxed_warning")),
        ("INDICATIONS AND USAGE", label.get("indications_and_usage")),
        ("DOSAGE AND ADMINISTRATION", label.get("dosage_and_administration")),
        ("WARNINGS AND PRECAUTIONS", label.get("warnings_and_precautions") or label.get("warnings")),
        ("CONTRAINDICATIONS", label.get("contraindications")),
        ("ADVERSE REACTIONS", label.get("adverse_reactions")),
        ("DRUG INTERACTIONS", label.get("drug_interactions")),
        ("MECHANISM OF ACTION", label.get("mechanism_of_action")),
        ("CLINICAL STUDIES", label.get("clinical_studies")),
    ]
    blocks: List[str] = []
    for header, content in sections:
        if not content:
            continue
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content if c)
        blocks.append(f"=== {header} ===\n{content}")
    return "\n\n".join(blocks) or "(empty label)"


# ---------------------------------------------------------------------------
# common
# ---------------------------------------------------------------------------

@dataclass
class _IngestOutcome:
    written: bool = False
    dedup_hit: bool = False
    skipped: bool = False
    error: bool = False
    document_id: Optional[str] = None


def _accumulate(result: IngestRunResult, outcome: _IngestOutcome) -> None:
    if outcome.error:
        result.errors += 1
    if outcome.skipped:
        result.documents_skipped += 1
    if outcome.written:
        result.documents_written += 1
        if outcome.document_id:
            result.written_ids.append(outcome.document_id)
    if outcome.dedup_hit:
        result.documents_dedup_hit += 1
