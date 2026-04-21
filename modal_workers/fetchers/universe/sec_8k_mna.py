"""SEC 8-K M&A fetcher for catalyst_universe.

Source: EDGAR full-text search
  https://efts.sec.gov/LATEST/search-index?forms=8-K&q=...

Maps each 8-K filing with item 1.01 (Entry into Material Definitive Agreement)
or 2.01 (Completion of Acquisition) in the window to a catalyst_universe row.

  item 1.01  →  profile=merger_arb, catalyst_type=mna_announce
  item 2.01  →  profile=merger_arb, catalyst_type=mna_close

Entity resolution via entity_identifiers (id_type=cik). CIK → primary_ticker
for the catalyst_universe.ticker column. Unresolved filers keep ticker=NULL;
raw_payload preserves CIK + company_name for the entity_linker pass.

SEC requires a User-Agent with contact info. Reads SEC_USER_AGENT env var,
which is set in Modal secret scanner-secrets per v2 memory.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... SEC_USER_AGENT="..." \\
    python3 -m modal_workers.fetchers.universe.sec_8k_mna \\
        --start-date 2026-04-01 --end-date 2026-04-21 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SOURCE_FEED = "edgar_8k_mna_search"
PAGE_SIZE = 100  # EDGAR efts cap

# EDGAR's full-text search can be filtered by form type + date range. We key on
# items mentioned in the filing text; the canonical phrases come from item
# headings in 8-K filings.
ITEM_QUERIES = {
    "mna_announce": '"Item 1.01" "Entry into a Material Definitive Agreement"',
    "mna_close":    '"Item 2.01" "Completion of Acquisition"',
}


def fetch(
    client: SupabaseClient,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch 8-K filings with item 1.01 or 2.01 in the window; upsert.

    Uses two separate queries (one per item type) rather than a single OR
    because EDGAR's search returns cleaner item-type attribution when the
    query string targets a single item.
    """
    fetched = 0
    upserted = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    session = _session()
    for catalyst_type, q in ITEM_QUERIES.items():
        offset = 0
        while True:
            try:
                r = session.get(
                    EDGAR_SEARCH_URL,
                    params={
                        "q": q,
                        "forms": "8-K",
                        "dateRange": "custom",
                        "startdt": start_date.isoformat(),
                        "enddt": end_date.isoformat(),
                        "from": str(offset),
                    },
                    timeout=30,
                )
                r.raise_for_status()
                body = r.json()
            except Exception as e:  # noqa: BLE001
                errors.append({"catalyst_type": catalyst_type, "offset": offset, "error": str(e)[:400]})
                break

            hits = (body.get("hits") or {}).get("hits") or []
            if not hits:
                break

            for hit in hits:
                fetched += 1
                row = _map_hit_to_row(hit, catalyst_type)
                if row is None:
                    skipped += 1
                    continue
                if dry_run:
                    upserted += 1
                    continue
                try:
                    cik = row["raw_payload"].get("cik")
                    # Two-pronged resolution: CIK → entity_identifiers (if populated)
                    # first, then fall back to the ticker parsed from display_names.
                    resolved_ticker, entity_id = _resolve_cik(client, cik)
                    ticker = resolved_ticker or row.get("ticker_from_display")
                    if entity_id is None and ticker:
                        entity_id = _resolve_ticker(client, ticker)
                    upsert_catalyst_universe_row(
                        client,
                        profile=row["profile"],
                        catalyst_type=row["catalyst_type"],
                        catalyst_date=row["catalyst_date"],
                        source_feed=row["source_feed"],
                        ticker=ticker,
                        entity_id=entity_id,
                        material_outcome=row["material_outcome"],
                        source_url=row.get("source_url"),
                        raw_payload=row["raw_payload"],
                    )
                    upserted += 1
                except (SupabaseError, ValueError) as e:
                    errors.append({
                        "catalyst_date": row["catalyst_date"],
                        "cik": row["raw_payload"].get("cik"),
                        "error": str(e)[:400],
                    })
                    skipped += 1

            offset += PAGE_SIZE
            if offset >= int((body.get("hits") or {}).get("total", {}).get("value", 0)):
                break
            # EDGAR rate limit: 10 req/sec sustained. Be polite.
            time.sleep(0.12)

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped": skipped,
        "errors": errors,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }


def _map_hit_to_row(hit: Dict[str, Any], catalyst_type: str) -> Optional[Dict[str, Any]]:
    """EDGAR search hit → catalyst_universe row. Returns None if malformed."""
    src = hit.get("_source") or {}
    # EDGAR returns filing date as yyyy-mm-dd in `file_date`. Display_names carry
    # "COMPANY NAME (CIK  0001234567) (SIC...)" — parse CIK out for entity lookup.
    file_date = src.get("file_date")
    if not file_date:
        return None
    try:
        filing_date = date.fromisoformat(file_date)
    except ValueError:
        return None

    display_names = src.get("display_names") or []
    first_name = display_names[0] if display_names else ""
    cik = _extract_cik(first_name)
    company_name = first_name.split(" (CIK")[0].strip() if first_name else None
    # EDGAR display_names carry ticker(s) when available, e.g.
    #   "Prelude Therapeutics Inc  (PRLD) (CIK 0001678660) (SIC 2836)"
    # The last "(XXXX)" before "(CIK" is the ticker cluster — may contain
    # comma-separated multiples (SPAC share classes, etc.). Take the first.
    ticker = _extract_ticker(first_name)

    adsh = src.get("adsh") or ""
    accession_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{adsh}-index.htm"
        if cik and adsh else None
    )

    return {
        "profile": "merger_arb",
        "catalyst_type": catalyst_type,
        "catalyst_date": filing_date.isoformat(),
        "source_feed": SOURCE_FEED,
        "material_outcome": "unclear",  # price-move deferred to entity_linker
        "source_url": accession_url,
        "ticker_from_display": ticker,  # passed through to upsert in fetch()
        "raw_payload": {
            "cik": cik,
            "company_name": company_name,
            "ticker_from_display": ticker,
            "adsh": adsh,
            "file_type": src.get("file_type"),
            "display_names": display_names,
        },
    }


def _extract_ticker(display_name: str) -> Optional[str]:
    """Parse first ticker from 'NAME  (TICKER[, TICKER2]) (CIK …) (SIC …)'.

    Returns None when no `(…)` cluster precedes `(CIK`. Multi-ticker clusters
    (SPAC share classes: RANG, RANGR, RANGU) collapse to the common stem.
    """
    if not display_name or "(CIK" not in display_name:
        return None
    pre_cik = display_name.split("(CIK")[0]
    # Find the LAST "(" in the pre-CIK slice — that's the ticker cluster.
    open_idx = pre_cik.rfind("(")
    close_idx = pre_cik.rfind(")")
    if open_idx < 0 or close_idx < 0 or close_idx < open_idx:
        return None
    cluster = pre_cik[open_idx + 1:close_idx].strip()
    if not cluster:
        return None
    first = cluster.split(",")[0].strip()
    # Defensive: reject anything that looks like SIC/CIK bleed-through.
    if not first or len(first) > 10 or first.isdigit():
        return None
    return first


def _extract_cik(display_name: str) -> Optional[str]:
    """Parse CIK out of 'COMPANY NAME (CIK  0001234567) (SIC ...)' form."""
    if "(CIK" not in display_name:
        return None
    try:
        after = display_name.split("(CIK")[1]
        digits = "".join(ch for ch in after.split(")")[0] if ch.isdigit())
        return digits.lstrip("0") or None  # strip leading zeros for FK match
    except (IndexError, ValueError):
        return None


_CIK_CACHE: Dict[str, tuple[Optional[str], Optional[str]]] = {}
_TICKER_CACHE: Dict[str, Optional[str]] = {}


def _resolve_ticker(client: SupabaseClient, ticker: str) -> Optional[str]:
    """ticker → entities.id via primary_ticker. Cached per-process."""
    if not ticker:
        return None
    if ticker in _TICKER_CACHE:
        return _TICKER_CACHE[ticker]
    try:
        rows = client._rest(
            "GET", "entities",
            params={"primary_ticker": f"eq.{ticker}", "select": "id", "limit": "1"},
        )
    except SupabaseError:
        _TICKER_CACHE[ticker] = None
        return None
    hit = rows[0]["id"] if rows else None
    _TICKER_CACHE[ticker] = hit
    return hit


def _resolve_cik(client: SupabaseClient, cik: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """CIK → (primary_ticker, entity_id). Cached per-process. Both None on miss."""
    if not cik:
        return None, None
    if cik in _CIK_CACHE:
        return _CIK_CACHE[cik]
    try:
        # entity_identifiers.id_type='cik', id_value=cik → entity_id
        ident_rows = client._rest(
            "GET", "entity_identifiers",
            params={"id_type": "eq.cik", "id_value": f"eq.{cik}",
                    "select": "entity_id", "limit": "1"},
        )
    except SupabaseError:
        _CIK_CACHE[cik] = (None, None)
        return None, None
    if not ident_rows:
        _CIK_CACHE[cik] = (None, None)
        return None, None
    entity_id = ident_rows[0]["entity_id"]
    try:
        ent_rows = client._rest(
            "GET", "entities",
            params={"id": f"eq.{entity_id}", "select": "primary_ticker", "limit": "1"},
        )
    except SupabaseError:
        _CIK_CACHE[cik] = (None, entity_id)
        return None, entity_id
    ticker = ent_rows[0].get("primary_ticker") if ent_rows else None
    _CIK_CACHE[cik] = (ticker, entity_id)
    return ticker, entity_id


def _session() -> requests.Session:
    s = requests.Session()
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        # EDGAR requires a User-Agent with contact. Callers in Modal get this
        # from scanner-secrets; local runs must set it explicitly.
        raise RuntimeError(
            "SEC_USER_AGENT env var required (see v2 memory: scanner-secrets). "
            'Format: "Name contact@example.com"'
        )
    s.headers.update({"User-Agent": ua, "Accept": "application/json"})
    return s


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=14)).isoformat())
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="Write to Supabase. Default dry-run.")
    args = parser.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)

    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(client, start_date=start, end_date=end, dry_run=not args.apply)
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
    def _rest(self, *a, **kw):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
