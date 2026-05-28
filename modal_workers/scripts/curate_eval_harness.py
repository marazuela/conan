"""Curate eval_harness rows from openFDA historical approvals.

Phase 0 deliverable. Pulls NDA/BLA original approvals (TYPE 1 NMEs) from
openFDA for 2023-2024, matches sponsor names against the `entities` table
for tickers, and inserts fda_assets + eval_harness rows.

CRL / rejection coverage:
  openFDA's /drug/drugsfda index does NOT publish CRL records — its
  submission_status enum is effectively {AP, TA, historical pre-1998 codes}.
  An earlier version of this script queried `submission_status:RL`, which
  matches zero rows (the FDA exposes CRL data only via the quarterly CRL
  Transparency table at open.fda.gov/apis/transparency/completeresponseletters,
  which is a PDF dump, not an API). CRL rows for eval_harness are therefore
  sourced from `curate_crl_from_edgar.py`, which mines CRL disclosures from
  EDGAR 8-K filing bodies and writes `realized_outcome='crl'` rows directly.

What this DOES:
  - Source: openFDA /drug/drugsfda for approvals (submission_status='AP')
    in 2023-01-01 .. 2024-12-31.
  - Filters: original submissions (submission_type IN ('ORIG','SUPPL')) with
    NDA/BLA classes (TYPE 1, TYPE 1 / Type 4, BLA, etc. — defined in
    HIGH_SIGNAL_CLASSES below). Excludes LABELING amendments and ANDA generics.
  - Matches sponsor_name against entities.name via ILIKE word-boundary search.
  - Inserts fda_assets row keyed on (ticker, drug_name, application_number) if
    not already present.
  - Inserts eval_harness row with realized_outcome='approved',
    reference_assessment_date = approval_date - 30 days, document_set=[].

What this DOES NOT (deferred):
  - Realized stock move (needs Polygon access — populate later).
  - Document set snapshotting (needs Phase 4 backfill — populate later via
    Federal Register + EDGAR ingestion adapters running over the historical
    window).

Run:
  python3 -m modal_workers.scripts.curate_eval_harness \\
      --since 2023-01-01 --until 2024-12-31 --max 100 [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.openfda_client import (
    openfda_auth_params,
    openfda_url,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# submission_class_code values worth ingesting (openFDA uses uppercase TYPE).
# TYPE 1 = New Molecular Entity. TYPE 4 = combination. BLA = any biologic.
# Excludes LABELING, MANUF (CMC), MEDGAS, REMS, UNKNOWN, etc.
HIGH_SIGNAL_CLASSES = {
    "TYPE 1",
    "TYPE 4",
    "BLA",
}

# submission_type values for "this is the actual approval moment".
ORIGINAL_TYPES = {"ORIG", "ORIG-1", "SUPPL"}

# submission_status: AP=approved. openFDA's drugsfda index does not expose
# CRL/refusal as a status code (see module docstring); CRL eval rows are
# sourced separately via curate_crl_from_edgar.py.
APPROVED_STATUSES = {"AP"}


@dataclass
class CandidateApproval:
    """One openFDA approval record, post-filtering."""
    application_number: str
    sponsor_name: str
    drug_brand: Optional[str]
    drug_generic: Optional[str]
    indication: Optional[str]                # not in drugsfda; resolved later from label
    submission_status: str                   # 'AP' (only AP is queried)
    submission_class_code: str
    submission_status_date: datetime         # approval moment
    raw_record: Dict[str, Any]


# ---------------------------------------------------------------------------
# openFDA fetch
# ---------------------------------------------------------------------------

def fetch_openfda_approvals(
    *,
    since: str = "20230101",
    until: str = "20241231",
    max_total: int = 100,
    page_size: int = 50,
) -> List[CandidateApproval]:
    """Pull approval (AP) records from /drug/drugsfda. CRLs are sourced
    separately via curate_crl_from_edgar.py (openFDA does not index CRL)."""
    candidates: List[CandidateApproval] = []
    skip = 0

    # openFDA Lucene search. TYPE 1 (NME) + Type 4 (combination) + BLA capture
    # the highest-signal originals; LABELING / MANUF / REMS amendments are
    # excluded by the class filter.
    class_filter = (
        '(submissions.submission_class_code:"TYPE 1" OR '
        'submissions.submission_class_code:"TYPE 4" OR '
        'submissions.submission_class_code:"BLA")'
    )
    status_filter = 'submissions.submission_status:AP'

    while len(candidates) < max_total:
        params = {
            "search": (
                f"{status_filter} AND {class_filter} AND "
                f"submissions.submission_status_date:[{since} TO {until}]"
            ),
            "limit": page_size,
            "skip": skip,
            **openfda_auth_params(),
        }
        url = openfda_url("drug/drugsfda.json")
        try:
            r = requests.get(url, params=params, timeout=30.0)
        except requests.exceptions.RequestException as exc:
            logger.error("openFDA request failed: %s", exc)
            break
        if r.status_code == 404:
            break
        if r.status_code != 200:
            logger.error("openFDA non-200: %d %s", r.status_code, r.text[:200])
            break

        body = r.json()
        results = body.get("results") or []
        if not results:
            break

        for app in results:
            cands = _candidates_from_record(app, since, until)
            candidates.extend(cands)
            if len(candidates) >= max_total:
                break

        if len(results) < page_size:
            break
        skip += page_size
        time.sleep(0.25)  # gentle pacing under openFDA rate limits

    return candidates[:max_total]


def _candidates_from_record(
    app: Dict[str, Any],
    since: str,
    until: str,
) -> List[CandidateApproval]:
    """Extract one CandidateApproval per qualifying submission in the record."""
    application_number = app.get("application_number")
    if not application_number:
        return []
    sponsor_name = app.get("sponsor_name") or "?"
    products = app.get("products") or []
    submissions = app.get("submissions") or []

    drug_brand: Optional[str] = None
    drug_generic: Optional[str] = None
    if products:
        p0 = products[0]
        drug_brand = p0.get("brand_name")
        active = (p0.get("active_ingredients") or [{}])[0]
        drug_generic = active.get("name")

    out: List[CandidateApproval] = []
    for s in submissions:
        sub_status = s.get("submission_status")
        if sub_status not in APPROVED_STATUSES:
            continue
        sub_type = s.get("submission_type")
        if sub_type not in ORIGINAL_TYPES:
            continue
        sub_class = s.get("submission_class_code") or ""
        if sub_class not in HIGH_SIGNAL_CLASSES:
            continue
        sub_date_str = s.get("submission_status_date")
        if not sub_date_str or not (since <= sub_date_str <= until):
            continue
        try:
            sub_date = datetime.strptime(sub_date_str, "%Y%m%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append(CandidateApproval(
            application_number=application_number,
            sponsor_name=sponsor_name,
            drug_brand=drug_brand,
            drug_generic=drug_generic,
            indication=None,
            submission_status=sub_status,
            submission_class_code=sub_class,
            submission_status_date=sub_date,
            raw_record=app,
        ))
    return out


# ---------------------------------------------------------------------------
# Sponsor → ticker matching via entities table
# ---------------------------------------------------------------------------
# Normalization helpers + fuzzy match consolidated into shared module D-110b.
# This module re-exports them under their original names so the rest of the
# script's logic (and any external callers) keep working unchanged.

from modal_workers.shared.sponsor_resolver import (
    _NORM_STRIP,           # noqa: F401  re-exported for backward compat
    _NORM_PUNCT,           # noqa: F401
    _GENERIC_TOKENS,       # noqa: F401
    _NON_SPONSOR_PATTERNS, # noqa: F401
    _normalize_sponsor,    # noqa: F401
    _distinctive_tokens,   # noqa: F401
    match_sponsor_to_ticker as _shared_match_sponsor_to_ticker,
)


def match_sponsor_to_ticker(
    sponsor_name: str,
    client: SupabaseClient,
) -> Optional[Dict[str, Any]]:
    """Fuzzy-match sponsor → entities row (with ticker). Delegates to the shared
    module so the resolver lives in one place (D-110b)."""
    return _shared_match_sponsor_to_ticker(sponsor_name, client)


# ---------------------------------------------------------------------------
# fda_assets upsert + eval_harness insert
# ---------------------------------------------------------------------------

def upsert_fda_asset(
    candidate: CandidateApproval,
    entity: Dict[str, Any],
    client: SupabaseClient,
) -> Optional[str]:
    """Find or create an fda_assets row. Returns asset_id."""
    ticker = entity.get("primary_ticker")
    if not ticker:
        return None

    drug_name = candidate.drug_brand or candidate.drug_generic or candidate.application_number

    # Look up by natural key (ticker, drug_name, application_number)
    existing = client._rest(
        "GET", "fda_assets",
        params={
            "select": "id",
            "ticker": f"eq.{ticker}",
            "drug_name": f"eq.{drug_name}",
            "application_number": f"eq.{candidate.application_number}",
            "limit": "1",
        },
    ) or []
    if existing:
        return existing[0]["id"]

    # Insert new row
    row = {
        "ticker": ticker,
        "mic": entity.get("primary_mic"),
        "entity_id": entity.get("id"),
        "drug_name": drug_name,
        "generic_name": candidate.drug_generic,
        "application_number": candidate.application_number,
        "application_type": "BLA" if candidate.submission_class_code == "BLA" else "NDA",
        "indication": None,                  # not available from drugsfda
        "sponsor_name": candidate.sponsor_name,
        "is_active": False,                  # historical, not in active watchlist
        "watch_priority": 4,                 # low for historical eval-only
        "extensions": {
            "source": "openfda_drugsfda",
            "submission_class_code": candidate.submission_class_code,
            "curated_for_eval_harness": True,
        },
    }
    try:
        rows = client._rest(
            "POST", "fda_assets",
            json_body=row,
            prefer="return=representation,resolution=ignore-duplicates",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("fda_assets insert failed for %s/%s: %s",
                         ticker, drug_name, exc)
        return None
    if not rows:
        return None
    return rows[0]["id"]


def insert_eval_harness_row(
    asset_id: str,
    candidate: CandidateApproval,
    client: SupabaseClient,
) -> bool:
    """Insert an eval_harness row for this candidate. Returns True on insert,
    False if dedupe-skipped or failed."""
    # openFDA drugsfda only yields AP records; CRL eval rows come from
    # curate_crl_from_edgar.py (see module docstring).
    realized_outcome = "approved"
    from datetime import timedelta
    reference_date = (candidate.submission_status_date.date() - timedelta(days=30))

    realized_outcome_data = {
        "source": "openfda_drugsfda",
        "curated_by": "curate_eval_harness_v0.1",
        "application_number": candidate.application_number,
        "submission_class_code": candidate.submission_class_code,
        "submission_status": candidate.submission_status,
        "approval_or_crl_date": candidate.submission_status_date.date().isoformat(),
        "drug_brand": candidate.drug_brand,
        "drug_generic": candidate.drug_generic,
        "sponsor_name": candidate.sponsor_name,
        "realized_move_pct": None,           # backfill via Polygon later
    }

    # Dedupe — skip if we've already curated this application_number/decision
    existing = client._rest(
        "GET", "eval_harness",
        params={
            "select": "id",
            "asset_id": f"eq.{asset_id}",
            "realized_outcome_data->>application_number": f"eq.{candidate.application_number}",
            "limit": "1",
        },
    ) or []
    if existing:
        logger.info("eval_harness: already curated app=%s; skipping",
                    candidate.application_number)
        return False

    row = {
        "asset_id": asset_id,
        "reference_assessment_date": reference_date.isoformat(),
        "realized_outcome": realized_outcome,
        "realized_outcome_data": realized_outcome_data,
        "document_set": [],
        "is_holdout": True,
        "difficulty": "medium",
        "notes": (
            f"Curated from openFDA drugsfda; {candidate.application_number} "
            f"{realized_outcome} on {candidate.submission_status_date.date()} "
            f"({candidate.submission_class_code}). Document set empty until "
            f"Phase 4 backfill from Federal Register + EDGAR for the asset's "
            f"30-day pre-decision window."
        ),
    }

    try:
        rows = client._rest(
            "POST", "eval_harness",
            json_body=row,
            prefer="return=minimal",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("eval_harness insert failed for asset %s: %s", asset_id, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@dataclass
class CurationStats:
    candidates_seen: int = 0
    matched_to_ticker: int = 0
    no_ticker_match: int = 0
    fda_assets_inserted: int = 0
    fda_assets_existing: int = 0
    eval_harness_inserted: int = 0
    errors: int = 0


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="curate_eval_harness")
    p.add_argument("--since", default="20230101", help="YYYYMMDD inclusive")
    p.add_argument("--until", default="20241231", help="YYYYMMDD inclusive")
    p.add_argument("--max", type=int, default=200,
                   help="Max candidate approvals to fetch from openFDA")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + match but don't insert")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = SupabaseClient()
    stats = CurationStats()
    skipped_for_review: List[Dict[str, Any]] = []

    logger.info("Fetching openFDA approvals %s..%s (max=%d)",
                args.since, args.until, args.max)
    candidates = fetch_openfda_approvals(
        since=args.since, until=args.until, max_total=args.max,
    )
    stats.candidates_seen = len(candidates)
    logger.info("Pulled %d candidate approval records", len(candidates))

    for cand in candidates:
        entity = match_sponsor_to_ticker(cand.sponsor_name, client)
        if not entity:
            stats.no_ticker_match += 1
            skipped_for_review.append({
                "application_number": cand.application_number,
                "sponsor_name": cand.sponsor_name,
                "drug": cand.drug_brand or cand.drug_generic,
                "status": cand.submission_status,
                "date": cand.submission_status_date.date().isoformat(),
                "reason": "no ticker match in entities table",
            })
            continue
        stats.matched_to_ticker += 1

        if args.dry_run:
            logger.info(
                "[dry-run] %s %s %s -> ticker=%s (%s)",
                cand.application_number, cand.submission_status,
                cand.drug_brand or cand.drug_generic,
                entity.get("primary_ticker"), entity.get("name"),
            )
            continue

        asset_id = upsert_fda_asset(cand, entity, client)
        if not asset_id:
            stats.errors += 1
            continue

        ok = insert_eval_harness_row(asset_id, cand, client)
        if ok:
            stats.eval_harness_inserted += 1
        else:
            stats.errors += 1

    logger.info(
        "Curation summary: candidates=%d matched=%d no_ticker=%d "
        "harness_inserted=%d errors=%d",
        stats.candidates_seen, stats.matched_to_ticker, stats.no_ticker_match,
        stats.eval_harness_inserted, stats.errors,
    )

    if skipped_for_review:
        # Print the first 30 unmatched for operator review
        logger.info("First 30 unmatched (no ticker in entities — "
                    "operator can add manually):")
        for s in skipped_for_review[:30]:
            print(f"  {s['date']} {s['status']} {s['application_number']:>14} "
                  f"{(s['drug'] or '?')[:40]:<40} {s['sponsor_name']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
