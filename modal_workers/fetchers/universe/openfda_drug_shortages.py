"""openFDA drug-shortage fetcher for catalyst_universe.

Source: openFDA's /drug/shortages endpoint (public, no auth, 240 req/min).
  https://api.fda.gov/drug/shortages.json
  https://open.fda.gov/apis/drug/drugshortages/how-to-use-the-endpoint/

An active US drug shortage is bearish for the holder: revenue impairment
(can't ship product) AND manufacturing-quality signal (likely 483 / CMC
issue behind the disruption). We map each `status=Current` record to a
catalyst_universe row at profile=binary_catalyst, catalyst_type=drug_shortage.

material_outcome is intentionally 'unclear' on emit — the schema CHECK
constraint admits only {yes, no, unclear}; the bearish *direction* lives in
raw_payload.expected_direction so downstream consumers (scoring profiles,
the materiality adjudicator) can treat it asymmetrically without needing
a new column. material_outcome will be flipped to yes/no later by the
price-move backfill once we know whether the shortage actually moved
the stock.

Dedup
-----
openFDA returns one record per (brand, strength, dosage_form, package)
tuple, so a single drug routinely surfaces as 5–15+ records (different
NDCs, vial sizes, etc.). We collapse them on (brand_or_generic, status)
BEFORE upsert so a 12-pack of strength variants doesn't fan out 12
catalyst_universe rows. The variant count is preserved in
raw_payload.variant_count for context.

fda_assets resolution
---------------------
Two-step lookup, best effort:
  1. openfda.application_number[0] → fda_assets.application_number (exact)
  2. proprietary_name (brand) → fda_assets.drug_name (ilike)
If both miss the row is counted as `skipped_no_asset` and NOT emitted —
this matches the curated-assets philosophy elsewhere in the v3 pipeline
(fed_register_adcom.py) and avoids polluting catalyst_universe with
NULL-ticker rows that would also duplicate on re-runs (the unique key
treats NULL ticker as distinct under PG's default NULLS DISTINCT).

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.openfda_drug_shortages \\
        --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402

OPENFDA_URL = "https://api.fda.gov/drug/shortages.json"
SOURCE_FEED = "openfda_drug_shortages"
ACTIVE_STATUS_QUERY = "status:Current"


def fetch(
    client: SupabaseClient,
    *,
    dry_run: bool = False,
    page_size: int = 100,
    max_pages: int = 50,
) -> Dict[str, Any]:
    """Fetch active US drug shortages, dedupe per drug, upsert catalyst_universe.

    No date window — the endpoint returns the current shortage list; status
    transitions (Current → Resolved) are tracked by re-running daily and
    relying on the upsert to update raw_payload on already-emitted rows.
    """
    fetched = 0
    deduped = 0
    upserted = 0
    skipped_no_asset = 0
    skipped_no_date = 0
    errors: List[Dict[str, Any]] = []

    raw_records: List[Dict[str, Any]] = []
    for page in range(max_pages):
        skip = page * page_size
        try:
            r = requests.get(
                OPENFDA_URL,
                params={"search": ACTIVE_STATUS_QUERY, "limit": page_size, "skip": skip},
                timeout=30,
            )
            if r.status_code == 404:
                break
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001
            errors.append({"page": page, "error": str(e)[:400]})
            break

        results = body.get("results") or []
        if not results:
            break
        raw_records.extend(results)
        fetched += len(results)

        meta = body.get("meta") or {}
        total = (meta.get("results") or {}).get("total")
        if total is not None and (skip + page_size) >= total:
            break

    clusters = _dedupe(raw_records)
    deduped = len(clusters)

    for cluster in clusters:
        rep = cluster["representative"]
        initial = _parse_mdy(rep.get("initial_posting_date"))
        if initial is None:
            skipped_no_date += 1
            continue

        proprietary_name = _proprietary_name(rep)
        application_number = _application_number(rep)

        asset = _lookup_fda_asset(client, application_number, proprietary_name)
        if asset is None:
            skipped_no_asset += 1
            continue
        ticker, entity_id = asset

        raw_payload = {
            "proprietary_name": proprietary_name,
            "generic_name": rep.get("generic_name"),
            "application_number": application_number,
            "company_name": rep.get("company_name"),
            "status": rep.get("status"),
            "update_type": rep.get("update_type"),
            "availability": rep.get("availability"),
            "shortage_reason": rep.get("shortage_reason"),
            "related_info": rep.get("related_info"),
            "resolved_note": rep.get("resolved_note"),
            "initial_posting_date": rep.get("initial_posting_date"),
            "change_date": rep.get("update_date"),
            "dosage_form": rep.get("dosage_form"),
            "presentation": rep.get("presentation"),
            "therapeutic_category": rep.get("therapeutic_category"),
            "variant_count": cluster["variant_count"],
            "expected_direction": "negative",
        }

        if dry_run:
            upserted += 1
            continue

        try:
            upsert_catalyst_universe_row(
                client,
                profile="binary_catalyst",
                catalyst_type="drug_shortage",
                catalyst_date=initial.isoformat(),
                source_feed=SOURCE_FEED,
                ticker=ticker,
                entity_id=entity_id,
                material_outcome="unclear",
                source_url=rep.get("related_info_link"),
                raw_payload=raw_payload,
            )
            upserted += 1
        except (SupabaseError, ValueError) as e:
            errors.append({
                "proprietary_name": proprietary_name,
                "application_number": application_number,
                "error": str(e)[:400],
            })

    return {
        "fetched": fetched,
        "deduped": deduped,
        "upserted": upserted,
        "skipped_no_asset": skipped_no_asset,
        "skipped_no_date": skipped_no_date,
        "errors": errors,
    }


# --------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------

def _dedupe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse (proprietary_name, status) clusters; pick the earliest-posted
    record as the cluster representative so catalyst_date reflects when the
    shortage event began rather than a later strength-variant filing."""
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rec in records:
        key = (_proprietary_name(rec).lower(), (rec.get("status") or "").lower())
        if not key[0]:
            continue
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = {"representative": rec, "variant_count": 1}
            continue
        existing["variant_count"] += 1
        cur = _parse_mdy(existing["representative"].get("initial_posting_date"))
        new = _parse_mdy(rec.get("initial_posting_date"))
        if new is not None and (cur is None or new < cur):
            existing["representative"] = rec
    return list(buckets.values())


def _proprietary_name(rec: Dict[str, Any]) -> str:
    """Brand name from openfda.brand_name[0]; fall back to generic_name."""
    brands = (rec.get("openfda") or {}).get("brand_name") or []
    if brands:
        return brands[0]
    return rec.get("generic_name") or ""


def _application_number(rec: Dict[str, Any]) -> Optional[str]:
    nums = (rec.get("openfda") or {}).get("application_number") or []
    return nums[0] if nums else None


def _parse_mdy(s: Optional[str]) -> Optional[date]:
    """openFDA posts dates as 'MM/DD/YYYY'; return None on anything weird."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


# --------------------------------------------------------------------
# fda_assets resolution
# --------------------------------------------------------------------

_ASSET_CACHE: Dict[str, Optional[Tuple[Optional[str], Optional[str]]]] = {}


def _lookup_fda_asset(
    client: SupabaseClient,
    application_number: Optional[str],
    proprietary_name: Optional[str],
) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Return (ticker, entity_id) when the shortage maps to an fda_assets row.

    Try application_number first (exact), then proprietary_name ilike against
    drug_name. Returns None when neither hits.

    Cached per-process so a 1000-record batch doesn't hit PostgREST 1000+
    times for the same drug.
    """
    cache_key = f"{application_number or ''}|{(proprietary_name or '').lower()}"
    if cache_key in _ASSET_CACHE:
        return _ASSET_CACHE[cache_key]

    hit: Optional[Tuple[Optional[str], Optional[str]]] = None
    if application_number:
        try:
            rows = client._rest(
                "GET", "fda_assets",
                params={
                    "application_number": f"eq.{application_number}",
                    "select": "ticker,entity_id",
                    "limit": "1",
                },
            )
            if rows:
                hit = (rows[0].get("ticker"), rows[0].get("entity_id"))
        except SupabaseError:
            pass

    if hit is None and proprietary_name:
        try:
            rows = client._rest(
                "GET", "fda_assets",
                params={
                    "drug_name": f"ilike.{proprietary_name}",
                    "select": "ticker,entity_id",
                    "limit": "1",
                },
            )
            if rows:
                hit = (rows[0].get("ticker"), rows[0].get("entity_id"))
        except SupabaseError:
            pass

    _ASSET_CACHE[cache_key] = hit
    return hit


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write to Supabase. Default dry-run.")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()

    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(
        client,
        dry_run=not args.apply,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    print(f"fetched (raw records):  {result['fetched']}")
    print(f"deduped (drug clusters): {result['deduped']}")
    print(f"upserted: {result['upserted']} ({'dry-run' if not args.apply else 'applied'})")
    print(f"skipped_no_asset: {result['skipped_no_asset']}")
    print(f"skipped_no_date:  {result['skipped_no_date']}")
    if result["errors"]:
        print(f"errors:   {len(result['errors'])}")
        for err in result["errors"][:5]:
            print(f"  - {err}")
    return 0


class _DryClient:
    """Stub for --no-apply runs — pretends nothing exists in fda_assets so
    every cluster falls into skipped_no_asset. Counters still validate dedup."""
    def _rest(self, *a, **kw):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
