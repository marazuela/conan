"""ClinicalTrials.gov v2 v3 ingestion path.

ClinicalTrials.gov is the canonical registry of US-jurisdiction clinical trials.
Each NCT ID corresponds to one trial. We ingest the structured trial record as
one document per NCT.

Used by the orchestrator (Stage 1) to anchor efficacy claims, verify trial
status, and check for design changes (e.g. enrollment slowed, primary endpoint
amended). The Sonnet extractor produces structured facts (phase, design,
endpoints, enrollment, status, milestones) from the raw_text payload.

API:
  v2 base: https://clinicaltrials.gov/api/v2/
  - /studies?query.term=...   — search trials by keyword/condition
  - /studies/{nctId}          — fetch one trial's full record

Public, unauthenticated. Rate-limited at ~50 req/min anonymous.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.document_writer import DocumentWriter, WriteResult

logger = logging.getLogger(__name__)

CT_BASE = "https://clinicaltrials.gov/api/v2"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_PAGE_SIZE = 50          # API caps at 1000; smaller pages = better incremental flush
MAX_PAGES_DEFAULT = 5


class ClinicalTrialsError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"clinicaltrials http {status}: {body[:200]}")
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

def _ct_get(path: str, params: Dict[str, Any], *,
            attempts: int = 3, backoff_s: float = 0.5,
            session: Optional[requests.Session] = None) -> Optional[Dict[str, Any]]:
    sess = session or requests.Session()
    url = f"{CT_BASE}/{path.lstrip('/')}"
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
            return None
        if r.status_code == 429 or r.status_code >= 500:
            last_exc = ClinicalTrialsError(r.status_code, r.text)
            if attempt < attempts - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            raise last_exc
        if r.status_code >= 400:
            raise ClinicalTrialsError(r.status_code, r.text)
        try:
            return r.json()
        except ValueError:
            return None
    if last_exc is not None:
        raise last_exc
    return None


# ---------------------------------------------------------------------------
# search by keyword (drug, sponsor, indication)
# ---------------------------------------------------------------------------

def ingest_search(
    query_term: str,
    *,
    phase: Optional[str] = None,            # e.g. 'PHASE3'
    status: Optional[str] = None,           # e.g. 'RECRUITING' / 'COMPLETED'
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES_DEFAULT,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Search ClinicalTrials.gov for `query_term` and ingest matching trials.

    Used by the asset linker / orchestrator to populate trial context for a
    drug or indication."""
    dw = writer or DocumentWriter()
    result = IngestRunResult()
    next_token: Optional[str] = None

    for _ in range(max_pages):
        params: Dict[str, Any] = {
            "query.term": query_term,
            "pageSize": page_size,
            "format": "json",
        }
        if phase:
            params["filter.advanced"] = f"AREA[Phase]{phase}"
        if status:
            params["filter.overallStatus"] = status
        if next_token:
            params["pageToken"] = next_token

        body = _ct_get("/studies", params=params)
        if not body:
            break
        studies = body.get("studies") or []
        if not studies:
            break

        result.documents_seen += len(studies)
        for s in studies:
            outcome = _ingest_study(s, dw)
            _accumulate(result, outcome)

        next_token = body.get("nextPageToken")
        if not next_token:
            break

    logger.info(
        "clinicaltrials search summary query=%r seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d",
        query_term, result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors)
    return result


# ---------------------------------------------------------------------------
# direct fetch by NCT id
# ---------------------------------------------------------------------------

def ingest_by_nct(
    nct_ids: List[str],
    *,
    writer: Optional[DocumentWriter] = None,
) -> IngestRunResult:
    """Fetch + write specific NCT IDs. Used by the asset linker to backfill
    trials referenced in an FDA approval letter or 8-K."""
    dw = writer or DocumentWriter()
    result = IngestRunResult()

    for nct in nct_ids:
        body = _ct_get(f"/studies/{nct}", params={"format": "json"})
        if not body:
            result.documents_skipped += 1
            continue
        result.documents_seen += 1
        outcome = _ingest_study(body, dw)
        _accumulate(result, outcome)

    logger.info(
        "clinicaltrials by_nct summary requested=%d seen=%d wrote=%d dedup=%d "
        "skipped=%d errors=%d",
        len(nct_ids), result.documents_seen, result.documents_written,
        result.documents_dedup_hit, result.documents_skipped, result.errors)
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _ingest_study(study: Dict[str, Any], writer: DocumentWriter) -> "_IngestOutcome":
    protocol = study.get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    nct_id = ident.get("nctId")
    if not nct_id:
        return _IngestOutcome(error=True)

    status = protocol.get("statusModule") or {}
    last_update = status.get("lastUpdateSubmitDate") or status.get("lastUpdatePostDateStruct", {}).get("date")
    if not last_update:
        return _IngestOutcome(skipped=True)

    try:
        published_at = datetime.fromisoformat(last_update).replace(tzinfo=timezone.utc)
    except ValueError:
        # Some entries use "YYYY-MM-DD" or "YYYY-MM" — handle both.
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                published_at = datetime.strptime(last_update, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return _IngestOutcome(error=True)

    raw_text = _format_study_as_text(study)
    title = ident.get("officialTitle") or ident.get("briefTitle") or nct_id
    sponsor = (protocol.get("sponsorCollaboratorsModule") or {}).get(
        "leadSponsor", {}).get("name")
    conditions = (protocol.get("conditionsModule") or {}).get("conditions") or []
    interventions = [
        i.get("name") for i in
        (protocol.get("armsInterventionsModule") or {}).get("interventions") or []
        if i.get("name")
    ]
    phase_list = (protocol.get("designModule") or {}).get("phases") or []

    extensions = {
        "nct_id": nct_id,
        "sponsor": sponsor,
        "conditions": conditions,
        "interventions": interventions,
        "phase": phase_list,
        "overall_status": status.get("overallStatus"),
        "primary_completion_date": (status.get("primaryCompletionDateStruct") or {}).get("date"),
        "study_first_post_date": (status.get("studyFirstPostDateStruct") or {}).get("date"),
        "last_update_submit_date": last_update,
    }

    try:
        result: WriteResult = writer.write_document(
            source="clinicaltrials",
            source_doc_id=nct_id,
            doc_type="clinical_trial",
            raw_text=raw_text,
            published_at=published_at,
            url=f"https://clinicaltrials.gov/study/{nct_id}",
            title=title,
            is_pdf=False,
            extensions=extensions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("clinicaltrials: write failed for %s: %s", nct_id, exc)
        return _IngestOutcome(error=True)

    return _IngestOutcome(
        written=result.was_new,
        dedup_hit=not result.was_new,
        document_id=result.document_id,
    )


def _format_study_as_text(study: Dict[str, Any]) -> str:
    """Flatten a ClinicalTrials.gov v2 record into a stable text block. The
    orchestrator's Sonnet extractor reads this and produces structured facts
    (phase, design, endpoints, enrollment, status milestones)."""
    protocol = study.get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    status = protocol.get("statusModule") or {}
    sponsor_mod = protocol.get("sponsorCollaboratorsModule") or {}
    cond_mod = protocol.get("conditionsModule") or {}
    design = protocol.get("designModule") or {}
    arms = protocol.get("armsInterventionsModule") or {}
    eligibility = protocol.get("eligibilityModule") or {}
    description = protocol.get("descriptionModule") or {}
    outcomes = protocol.get("outcomesModule") or {}

    lines: List[str] = [
        f"NCT ID: {ident.get('nctId', '?')}",
        f"Title: {ident.get('officialTitle') or ident.get('briefTitle', '?')}",
        f"Sponsor: {sponsor_mod.get('leadSponsor', {}).get('name', '?')}",
        f"Status: {status.get('overallStatus', '?')}",
        f"Phase: {', '.join(design.get('phases') or []) or '?'}",
        f"Study Type: {design.get('studyType', '?')}",
        f"Conditions: {', '.join(cond_mod.get('conditions') or []) or '?'}",
        "",
    ]

    # Brief & detailed description
    if description.get("briefSummary"):
        lines.append("=== Brief Summary ===")
        lines.append(description["briefSummary"])
        lines.append("")
    if description.get("detailedDescription"):
        lines.append("=== Detailed Description ===")
        lines.append(description["detailedDescription"])
        lines.append("")

    # Interventions
    interventions = arms.get("interventions") or []
    if interventions:
        lines.append("=== Interventions ===")
        for i in interventions:
            lines.append(
                f"- {i.get('type', '?')}: {i.get('name', '?')}"
                + (f" — {i.get('description', '')}" if i.get('description') else "")
            )
        lines.append("")

    # Outcomes (primary + secondary endpoints)
    primary = outcomes.get("primaryOutcomes") or []
    if primary:
        lines.append("=== Primary Outcomes ===")
        for p in primary:
            lines.append(
                f"- {p.get('measure', '?')} "
                f"(timeframe: {p.get('timeFrame', '?')})"
                + (f" — {p.get('description', '')}" if p.get('description') else "")
            )
        lines.append("")
    secondary = outcomes.get("secondaryOutcomes") or []
    if secondary:
        lines.append("=== Secondary Outcomes ===")
        for s in secondary[:10]:  # cap at 10 — many trials have 30+
            lines.append(
                f"- {s.get('measure', '?')} "
                f"(timeframe: {s.get('timeFrame', '?')})"
            )
        if len(secondary) > 10:
            lines.append(f"  (+{len(secondary) - 10} more)")
        lines.append("")

    # Enrollment & eligibility
    enrollment = (design.get("enrollmentInfo") or {}).get("count")
    if enrollment is not None:
        lines.append(f"Enrollment: {enrollment}")
    if eligibility.get("eligibilityCriteria"):
        lines.append("=== Eligibility ===")
        # Eligibility text can be huge — cap at 3000 chars to keep doc size sane.
        crit = eligibility["eligibilityCriteria"]
        if len(crit) > 3000:
            crit = crit[:3000] + "\n[truncated]"
        lines.append(crit)
        lines.append("")

    # Status milestones
    if status.get("studyFirstPostDateStruct"):
        lines.append(f"First posted: {status['studyFirstPostDateStruct'].get('date', '?')}")
    if status.get("primaryCompletionDateStruct"):
        lines.append(f"Primary completion (est): {status['primaryCompletionDateStruct'].get('date', '?')}")
    if status.get("completionDateStruct"):
        lines.append(f"Study completion (est): {status['completionDateStruct'].get('date', '?')}")
    if status.get("lastUpdateSubmitDate"):
        lines.append(f"Last update submitted: {status['lastUpdateSubmitDate']}")

    return "\n".join(lines)


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
