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
from modal_workers.shared.sponsor_resolver import resolve_sponsor

logger = logging.getLogger(__name__)

OPENFDA_BASE = "https://api.fda.gov"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_PAGE_LIMIT = 100  # openFDA caps at 1000 per request, but smaller pages
                          # play better with the rate limit + give incremental progress

# Safety ceiling for the page-until-empty loop. 100 × 100 = 10k records per
# feed per run, well above any realistic openFDA bulk-publish batch. If a paging
# bug ever made the API return full pages indefinitely this caps the blast radius.
MAX_PAGES_HARD_CAP = 100

# Wider lookback used by deep_sweep_openfda to catch corrections / backfills
# that the default 30d window slid past.
DEEP_SWEEP_DAYS = 180


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
    max_pages: Optional[int] = None,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull NDA/BLA approval records from openFDA drug/drugsfda.

    Each application_number → one document. raw_text is a JSON-formatted summary
    of the application's submission history (Sonnet extractor handles structuring
    downstream). source_doc_id = application_number.

    Pagination: defaults to page-until-empty (a short page or no results ends
    the loop) bounded by MAX_PAGES_HARD_CAP. Pass `max_pages` to cap a backfill
    where you only want the first N pages.
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
    page_cap = max_pages if max_pages is not None else MAX_PAGES_HARD_CAP

    pages_fetched = 0
    for page_idx in range(page_cap):
        body = _openfda_get(
            "/drug/drugsfda.json",
            params={"search": search, "limit": page_limit, "skip": skip},
        )
        pages_fetched = page_idx + 1
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

    if max_pages is None and pages_fetched >= MAX_PAGES_HARD_CAP:
        logger.warning(
            "openfda drugsfda hit MAX_PAGES_HARD_CAP=%d on search=%r — "
            "possible bulk-publish batch larger than the safety ceiling; "
            "investigate before widening the cap",
            MAX_PAGES_HARD_CAP, search)

    logger.info(
        "openfda drugsfda summary search=%r seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d pages=%d",
        search, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors,
        pages_fetched)
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

    # D-110b: resolve sponsor → ticker via curated map (Pass 1 only at ingest;
    # the Jaccard fallback round-trips to Supabase and is reserved for a later
    # batch-resolve pass to keep ingest hot-path fast). Curated misses persist
    # nullable ticker + match_method='unresolved'; an offline batch can re-run
    # resolve_sponsor with skip_jaccard=False to fill the tail.
    resolution = resolve_sponsor(sponsor, client=None, skip_jaccard=True)

    extensions = {
        "application_number": application_number,
        "sponsor_name": sponsor,
        "sponsor_resolution": {
            "ticker": resolution.ticker,
            "mic": resolution.mic,
            "country": resolution.country,
            "match_method": resolution.match_method,
            "confidence": resolution.confidence,
            "tradeable": resolution.tradeable,
        },
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
    max_pages: Optional[int] = None,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Pull recent drug-label changes (effective_time within window). Each
    setid (label revision) becomes one document.

    Source field is 'dailymed' (canonical home of structured product labels)
    to keep the source taxonomy aligned with the orchestrator's expectations.

    Pagination: defaults to page-until-empty bounded by MAX_PAGES_HARD_CAP.
    """
    today = date.today()
    since = since or (today - timedelta(days=30))
    until = until or today

    search = (f"effective_time:[{since.strftime('%Y%m%d')} "
              f"TO {until.strftime('%Y%m%d')}]")
    dw = writer or DocumentWriter()
    result = IngestRunResult()
    skip = 0
    page_cap = max_pages if max_pages is not None else MAX_PAGES_HARD_CAP

    pages_fetched = 0
    for page_idx in range(page_cap):
        body = _openfda_get(
            "/drug/label.json",
            params={"search": search, "limit": page_limit, "skip": skip},
        )
        pages_fetched = page_idx + 1
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

    if max_pages is None and pages_fetched >= MAX_PAGES_HARD_CAP:
        logger.warning(
            "openfda label hit MAX_PAGES_HARD_CAP=%d on since=%s — "
            "possible bulk-publish batch larger than the safety ceiling; "
            "investigate before widening the cap",
            MAX_PAGES_HARD_CAP, since)

    logger.info(
        "openfda label summary since=%s seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d pages=%d",
        since, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors,
        pages_fetched)
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

    # Best-effort: seed fda_assets.mechanism from the label's MoA when the
    # asset row still has NULL. Never clobbers an operator-curated value.
    # Errors here must NOT fail the ingest — log and move on.
    try:
        _maybe_seed_asset_mechanism(writer.client, label)
    except Exception:  # noqa: BLE001
        logger.exception("openfda label: mechanism seed pass raised for set_id=%s", set_id)

    return _IngestOutcome(
        written=result.was_new,
        dedup_hit=not result.was_new,
        document_id=result.document_id,
    )


def extract_mechanism_from_label(label: Dict[str, Any]) -> str:
    """Pull a single normalized MoA string from an openFDA `drug/label` record.

    openFDA returns `mechanism_of_action` as a list of strings (one entry per
    label-section paragraph). We join with whitespace, trim, and cap at 1024
    characters — class-precedent normalization is `LOWER(TRIM(...))`, so
    multi-kilobyte blobs add no signal. Returns "" when MoA is absent.
    """
    raw = label.get("mechanism_of_action")
    if isinstance(raw, list):
        parts = [s for s in raw if isinstance(s, str) and s.strip()]
        text = " ".join(p.strip() for p in parts)
    elif isinstance(raw, str):
        text = raw
    else:
        return ""
    text = " ".join(text.split())  # collapse runs of whitespace
    return text[:1024]


def _maybe_seed_asset_mechanism(client: Any, label: Dict[str, Any]) -> int:
    """Upsert fda_assets.mechanism from this label where the asset row currently
    has NULL. Match strategy is application_number ∈ openfda.application_number;
    one label can map to multiple fda_assets rows (the unique key is
    (ticker, drug_name, application_number) so per-ticker duplicates are
    expected and intentional). Returns count of asset rows touched.
    """
    openfda_meta = label.get("openfda") or {}
    application_numbers = sorted({
        a.strip() for a in (openfda_meta.get("application_number") or [])
        if isinstance(a, str) and a.strip()
    })
    if not application_numbers:
        return 0

    moa = extract_mechanism_from_label(label)
    if not moa:
        return 0

    in_clause = "(" + ",".join(f'"{n}"' for n in application_numbers) + ")"
    rows = client._rest_with_retry(
        "PATCH",
        "fda_assets",
        params={
            "application_number": f"in.{in_clause}",
            "mechanism": "is.null",
            "select": "id",
        },
        json_body={"mechanism": moa},
        prefer="return=representation",
    )
    n_updated = len(rows or [])
    if n_updated:
        logger.info(
            "openfda label: seeded mechanism on %d fda_assets row(s) "
            "(application_numbers=%s, moa_chars=%d)",
            n_updated, application_numbers, len(moa))
    return n_updated


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


# ---------------------------------------------------------------------------
# Deep sweep — wider-window catch-up for backfills and corrections that the
# default 30d sliding window cannot see.
# ---------------------------------------------------------------------------

def deep_sweep_openfda(
    *,
    days: int = DEEP_SWEEP_DAYS,
    writer: Optional[DocumentWriter] = None,
) -> Dict[str, IngestRunResult]:
    """Wider-window catch-up across drugsfda + dailymed labels.

    openFDA emits corrections to old records (visible via `_meta.last_updated`
    on the API side). The default 30d sliding window in the daily ingest path
    cannot see corrections older than 30d, leaving stale fact extractions in
    the RAG corpus. This helper runs the same ingest with `since = today - 180d`
    and `max_pages=None` so any corrected records re-flow through DocumentWriter,
    where idempotent content_hash dedupe makes already-seen rows cheap no-ops.

    Returns a dict keyed by feed name so the caller can log per-feed counts.
    """
    today = date.today()
    since = today - timedelta(days=days)
    dw = writer or DocumentWriter()
    drugsfda = ingest_drugsfda_approvals(since=since, until=today, writer=dw)
    label = ingest_drug_label_recent(since=since, until=today, writer=dw)
    logger.info(
        "openfda deep_sweep days=%d drugsfda(seen=%d wrote=%d dedup=%d) "
        "label(seen=%d wrote=%d dedup=%d)",
        days,
        drugsfda.documents_seen, drugsfda.documents_written,
        drugsfda.documents_dedup_hit,
        label.documents_seen, label.documents_written,
        label.documents_dedup_hit)
    return {"drugsfda": drugsfda, "label": label}
