"""FDA drug-approval fetcher for catalyst_universe.

Source: openFDA's /drug/drugsfda endpoint (public, no auth, 240 req/min).
  https://api.fda.gov/drug/drugsfda.json

Maps each AP (Approved) or TA (Tentative Approval) submission with a
submission_status_date in the window to a catalyst_universe row at
profile=binary_catalyst, catalyst_type=fda_approval. CRL outcomes
(catalyst_type=fda_crl) are NOT available via this endpoint — they surface
through FDA news releases instead, tracked separately.

Ticker resolution is best-effort: we match sponsor_name against
entities.name and fall back to NULL when no hit. The coverage auditor
tolerates NULL ticker (it can still bucket by profile + date + sponsor_name
in raw_payload). An entity_linker pass will backfill tickers later.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fda_adcomm_pdufa \\
        --start-date 2026-01-01 --end-date 2026-04-21 --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402

OPENFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
SOURCE_FEED = "openfda_drugsfda"


def fetch(
    client: SupabaseClient,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
    page_size: int = 100,
    max_pages: int = 50,
) -> Dict[str, Any]:
    """Fetch FDA approvals with submission_status_date in [start_date, end_date].

    openFDA's elasticsearch query shape: `submissions.submission_status_date:[YYYYMMDD+TO+YYYYMMDD]
    +AND+submissions.submission_status:AP`. Pagination via skip=.
    """
    fetched = 0
    upserted = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    search = (
        f"submissions.submission_status_date:[{_d(start_date)}+TO+{_d(end_date)}]"
        f"+AND+submissions.submission_status:AP"
    )

    # Build search URL manually — `requests.params` encodes `+` as `%2B` but
    # openFDA's elasticsearch range query needs literal `+` between tokens.
    # quote(..., safe="+:") keeps `+` and `:` raw while escaping `[`, `]` etc.
    search_enc = quote(search, safe="+:")

    for page in range(max_pages):
        skip = page * page_size
        url = f"{OPENFDA_URL}?search={search_enc}&limit={page_size}&skip={skip}"
        try:
            r = requests.get(url, timeout=30)
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

        for drug in results:
            fetched += 1
            rows = _map_drug_to_rows(drug, start_date, end_date)
            if not rows:
                skipped += 1
                continue
            for row in rows:
                if dry_run:
                    upserted += 1
                    continue
                try:
                    entity_id = _lookup_entity_id(client, row["raw_payload"].get("sponsor_name"))
                    upsert_catalyst_universe_row(
                        client,
                        profile=row["profile"],
                        catalyst_type=row["catalyst_type"],
                        catalyst_date=row["catalyst_date"],
                        source_feed=row["source_feed"],
                        ticker=row.get("ticker"),
                        entity_id=entity_id,
                        material_outcome=row["material_outcome"],
                        source_url=row.get("source_url"),
                        raw_payload=row["raw_payload"],
                    )
                    upserted += 1
                except (SupabaseError, ValueError) as e:
                    errors.append({
                        "catalyst_date": row["catalyst_date"],
                        "sponsor": row["raw_payload"].get("sponsor_name"),
                        "error": str(e)[:400],
                    })
                    skipped += 1

        # Short-circuit when the server signaled this was the last page.
        meta = body.get("meta") or {}
        total = (meta.get("results") or {}).get("total")
        if total is not None and (skip + page_size) >= total:
            break

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped": skipped,
        "errors": errors,
        "window": {"start": _d(start_date), "end": _d(end_date)},
    }


def _map_drug_to_rows(drug: Dict[str, Any], start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """openFDA drug → one catalyst_universe row per AP submission in window.

    A single drugsfda record may include multiple submissions over time
    (original approval, supplements, efficacy supplements). Each AP
    submission in our window becomes its own catalyst.
    """
    rows: List[Dict[str, Any]] = []
    sponsor = drug.get("sponsor_name") or "UNKNOWN"
    application_number = drug.get("application_number") or ""

    # Extract brand + generic names from the first product; drugs@fda often
    # carries the same set across products within one application.
    products = drug.get("products") or []
    brand = products[0].get("brand_name") if products else None
    generic = products[0].get("active_ingredients", [{}])[0].get("name") if products else None

    for sub in drug.get("submissions") or []:
        if sub.get("submission_status") != "AP":
            continue
        raw = sub.get("submission_status_date") or ""
        if len(raw) != 8 or not raw.isdigit():
            continue
        approved = date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        if not (start_date <= approved <= end_date):
            continue

        rows.append({
            "profile": "binary_catalyst",
            "catalyst_type": "fda_approval",
            "catalyst_date": approved.isoformat(),
            "source_feed": SOURCE_FEED,
            "ticker": None,   # filled by entity resolution below
            "material_outcome": "unclear",  # price-move computation deferred
            "source_url": (
                f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
                f"?event=overview.process&ApplNo={application_number}"
                if application_number else None
            ),
            "raw_payload": {
                "sponsor_name": sponsor,
                "application_number": application_number,
                "brand_name": brand,
                "generic_name": generic,
                "submission_type": sub.get("submission_type"),
                "submission_number": sub.get("submission_number"),
            },
        })

    return rows


# --------------------------------------------------------------------
# Entity resolution (best-effort)
# --------------------------------------------------------------------

_ENTITY_CACHE: Dict[str, Optional[str]] = {}


def _lookup_entity_id(client: SupabaseClient, sponsor_name: Optional[str]) -> Optional[str]:
    """Match sponsor_name against entities.name via ilike. None if no hit.

    Cached per-process so a 50-row batch doesn't hit PostgREST 50 times for
    the same sponsor (common pattern: Pfizer files 10+ drugs/year).
    """
    if not sponsor_name:
        return None
    if sponsor_name in _ENTITY_CACHE:
        return _ENTITY_CACHE[sponsor_name]
    try:
        # Strip suffixes like ", INC.", ", LLC" for ilike match.
        key = sponsor_name.split(",")[0].strip().lower()
        rows = client._rest(
            "GET", "entities",
            params={"name": f"ilike.%{key}%", "select": "id", "limit": "1"},
        )
    except SupabaseError:
        _ENTITY_CACHE[sponsor_name] = None
        return None
    hit = rows[0]["id"] if rows else None
    _ENTITY_CACHE[sponsor_name] = hit
    return hit


def _d(d: date) -> str:
    return d.strftime("%Y%m%d")


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=30)).isoformat())
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="Write to Supabase. Default dry-run.")
    parser.add_argument("--max-pages", type=int, default=50)
    args = parser.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)

    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(client, start_date=start, end_date=end,
                   dry_run=not args.apply, max_pages=args.max_pages)
    print(f"window:   {result['window']['start']} → {result['window']['end']}")
    print(f"fetched:  {result['fetched']}")
    print(f"upserted: {result['upserted']} ({'dry-run' if not args.apply else 'applied'})")
    print(f"skipped:  {result['skipped']}")
    if result["errors"]:
        print(f"errors:   {len(result['errors'])}")
        for err in result["errors"][:5]:
            print(f"  - {err}")
    return 0


class _DryClient:
    """Stub for --no-apply runs — supports the client interface but never calls REST."""
    def _rest(self, *a, **kw):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
