"""FDA drug/biologic inspection-classification fetcher.

Populates `public.fda_drug_inspections` — the source for the NDA CRL rubric's
`n_drug_inspections_5y` feature (count of a sponsor's drug/biologic facility
inspections in a trailing 5-year window).

Source: FDA Inspections Classification dataset (FDA Data Dashboard / ORA).
  https://datadashboard.fda.gov/ora/cd/inspections.htm
  API base (validate auth/endpoint at deploy time): the dashboard exposes
  inspection-classification records as JSON; field names below are read
  defensively with aliases so a minor schema shift does not silently drop rows.

The HTTP call is injectable (`fetch_raw`) so the parse/normalize/upsert path is
unit-tested against fixtures without network access. Firm legal names are
matched to drug sponsors best-effort via resolve_sponsor; the trailing-5y
windowing happens in feature-assembly, not here.

Run locally (writes):
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fda_inspections --apply
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient  # noqa: E402
from modal_workers.shared.sponsor_resolver import resolve_sponsor  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "fda_inspections_classification"
PRODUCT_TYPES_OF_INTEREST = {"DRUGS", "BIOLOGICS"}
DEFAULT_LOOKBACK_YEARS = 6  # >5y so the trailing-5y window always has margin


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
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text if fmt != "%Y%m%d" else str(value)[:8], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _inspection_id(fei: Optional[str], firm_norm: str, end_date: Optional[str],
                   classification: Optional[str], product_type: Optional[str]) -> str:
    basis = "|".join([
        str(fei or firm_norm),
        str(end_date or ""),
        str(classification or ""),
        str(product_type or ""),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def parse_inspection_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one dashboard inspection record into an fda_drug_inspections row.

    Returns None for non-drug/biologic records or records missing a firm name.
    Pure — no I/O. Reads dashboard field names with common aliases.
    """
    firm = _first(raw, "LegalName", "legal_name", "FirmLegalName", "firm_name", "Firm")
    if not firm:
        return None
    product_type = _first(raw, "ProductType", "product_type", "Program")
    if product_type is not None and str(product_type).strip().upper() not in PRODUCT_TYPES_OF_INTEREST:
        return None

    firm_norm = normalize_firm(firm)
    fei = _first(raw, "FEINumber", "fei_number", "FEI")
    end_date = _parse_date(_first(raw, "InspectionEndDate", "inspection_end_date", "EndDate"))
    classification = _first(raw, "ClassificationCode", "Classification", "classification")
    posted = _first(raw, "PostedCitations", "posted_citations")
    posted_bool = None
    if posted is not None:
        posted_bool = str(posted).strip().upper() in ("Y", "YES", "TRUE", "1")

    return {
        "inspection_id": _inspection_id(
            str(fei) if fei is not None else None, firm_norm, end_date,
            str(classification) if classification is not None else None,
            str(product_type) if product_type is not None else None,
        ),
        "fei_number": str(fei) if fei is not None else None,
        "firm_name": str(firm),
        "firm_name_norm": firm_norm,
        "inspection_end_date": end_date,
        "classification": str(classification) if classification is not None else None,
        "product_type": str(product_type) if product_type is not None else None,
        "posted_citations": posted_bool,
        "source": SOURCE,
    }


def _resolve_ticker(firm: str) -> Optional[str]:
    try:
        return resolve_sponsor(firm, client=None, skip_jaccard=True).ticker
    except Exception:  # noqa: BLE001
        return None


def _default_fetch_raw(lookback_years: int) -> List[Dict[str, Any]]:
    """Live HTTP fetch of inspection-classification records.

    Placeholder for the dashboard call — the exact endpoint + auth must be
    confirmed at deploy time (see module docstring). Kept isolated so the
    tested code path (parse/normalize/upsert) does not depend on it.
    """
    raise NotImplementedError(
        "Live FDA Data Dashboard inspections fetch not wired — confirm endpoint "
        "+ auth at deploy time, then implement _default_fetch_raw. The parse and "
        "upsert paths are validated independently via fixtures."
    )


def fetch(
    client: SupabaseClient,
    *,
    dry_run: bool = False,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    fetch_raw: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Fetch inspection classifications, normalize, resolve sponsor, upsert."""
    raw_records = (fetch_raw or _default_fetch_raw)(lookback_years)
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    skipped = 0
    for raw in raw_records:
        row = parse_inspection_record(raw)
        if row is None:
            skipped += 1
            continue
        if row["inspection_id"] in seen:
            continue
        seen.add(row["inspection_id"])
        row["sponsor_ticker"] = _resolve_ticker(row["firm_name"])
        rows.append(row)

    if not dry_run and rows:
        client._rest_with_retry(
            "POST", "fda_drug_inspections",
            json_body=rows,
            params={"on_conflict": "inspection_id"},
            prefer="resolution=merge-duplicates,return=minimal",
        )
    logger.info("fda_inspections: parsed=%d skipped=%d upserted=%d dry_run=%s",
                len(rows), skipped, 0 if dry_run else len(rows), dry_run)
    return {"parsed": len(rows), "skipped": skipped,
            "upserted": 0 if dry_run else len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FDA drug inspection classifications.")
    parser.add_argument("--apply", action="store_true", help="write rows (default dry-run)")
    parser.add_argument("--lookback-years", type=int, default=DEFAULT_LOOKBACK_YEARS)
    args = parser.parse_args()
    result = fetch(SupabaseClient(), dry_run=not args.apply, lookback_years=args.lookback_years)
    print(result)


if __name__ == "__main__":
    main()
