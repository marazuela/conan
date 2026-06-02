"""FDA Warning Letters fetcher.

Populates `public.fda_warning_letters` — the source for the NDA CRL rubric's
`sponsor_has_warning` feature (sponsor has >=1 prior FDA warning letter).

Source: FDA Warning Letters dataset (fda.gov / FDA Data Dashboard compliance
actions). `documents.source='fda_warning_letter'` already captures letters at
the document level; this fetcher adds the entity-linked feature view.

The HTTP call is injectable (`fetch_raw`) so the parse/normalize/upsert path is
unit-tested against fixtures without network access. Firm legal names are
matched to drug sponsors best-effort via resolve_sponsor.

Run locally (writes):
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fda_warning_letters --apply
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient  # noqa: E402
from modal_workers.shared.sponsor_resolver import resolve_sponsor  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "fda_warning_letters"
DEFAULT_LOOKBACK_YEARS = 10  # sponsor history is "has ever" — keep a wide window


def normalize_firm(name: object) -> str:
    """LOWER(TRIM(collapse interior whitespace)) — matches firm_name_norm."""
    return " ".join(str(name or "").strip().lower().split())


def _first(raw: Dict[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _parse_date(value: object) -> Optional[str]:
    if not value:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _letter_id(firm_norm: str, issue_date: Optional[str], subject: Optional[str]) -> str:
    basis = "|".join([firm_norm, str(issue_date or ""), str(subject or "")])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def parse_warning_letter_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one warning-letter record into an fda_warning_letters row.

    Returns None when the firm name is missing. Pure — no I/O. Reads common
    field-name aliases so a minor source schema shift does not drop rows.
    """
    firm = _first(raw, "companyName", "LegalName", "firm_name", "Firm", "company")
    if not firm:
        return None
    firm_norm = normalize_firm(firm)
    issue_date = _parse_date(_first(raw, "letterIssueDate", "issueDate", "issue_date", "PostedDate"))
    subject = _first(raw, "subject", "Subject", "letterType")
    return {
        "letter_id": _letter_id(firm_norm, issue_date, str(subject) if subject else None),
        "firm_name": str(firm),
        "firm_name_norm": firm_norm,
        "issue_date": issue_date,
        "letter_url": _first(raw, "letterURL", "url", "Link"),
        "issuing_office": _first(raw, "issuingOffice", "office", "IssuingOffice"),
        "subject": str(subject) if subject else None,
        "source": SOURCE,
    }


def _resolve_ticker(firm: str) -> Optional[str]:
    try:
        return resolve_sponsor(firm, client=None, skip_jaccard=True).ticker
    except Exception:  # noqa: BLE001
        return None


def _default_fetch_raw(lookback_years: int) -> List[Dict[str, Any]]:
    """Live HTTP fetch of warning-letter records.

    Placeholder — confirm the FDA source endpoint at deploy time (see module
    docstring). Kept isolated so the tested parse/upsert path is independent.
    """
    raise NotImplementedError(
        "Live FDA Warning Letters fetch not wired — confirm source endpoint at "
        "deploy time, then implement _default_fetch_raw. Parse and upsert paths "
        "are validated independently via fixtures."
    )


def fetch(
    client: SupabaseClient,
    *,
    dry_run: bool = False,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    fetch_raw: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Fetch warning letters, normalize, resolve sponsor, upsert."""
    raw_records = (fetch_raw or _default_fetch_raw)(lookback_years)
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    skipped = 0
    for raw in raw_records:
        row = parse_warning_letter_record(raw)
        if row is None:
            skipped += 1
            continue
        if row["letter_id"] in seen:
            continue
        seen.add(row["letter_id"])
        row["sponsor_ticker"] = _resolve_ticker(row["firm_name"])
        rows.append(row)

    if not dry_run and rows:
        client._rest_with_retry(
            "POST", "fda_warning_letters",
            json_body=rows,
            params={"on_conflict": "letter_id"},
            prefer="resolution=merge-duplicates,return=minimal",
        )
    logger.info("fda_warning_letters: parsed=%d skipped=%d upserted=%d dry_run=%s",
                len(rows), skipped, 0 if dry_run else len(rows), dry_run)
    return {"parsed": len(rows), "skipped": skipped,
            "upserted": 0 if dry_run else len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FDA Warning Letters.")
    parser.add_argument("--apply", action="store_true", help="write rows (default dry-run)")
    parser.add_argument("--lookback-years", type=int, default=DEFAULT_LOOKBACK_YEARS)
    args = parser.parse_args()
    result = fetch(SupabaseClient(), dry_run=not args.apply, lookback_years=args.lookback_years)
    print(result)


if __name__ == "__main__":
    main()
