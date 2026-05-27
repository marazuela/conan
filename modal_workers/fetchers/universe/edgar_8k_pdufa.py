"""EDGAR 8-K PDUFA fetcher → fda_regulatory_events.

Source: EDGAR full-text search
  https://efts.sec.gov/LATEST/search-index?forms=8-K&q=...

Maps each 8-K filing whose text contains a forward-looking PDUFA-date phrase
to an `fda_regulatory_events` row at event_type='pdufa', event_status='pending'.

Forward-looking PDUFA dates have NO clean structured public source — openFDA's
drugsfda endpoint exposes only post-decision submissions, the FDA does not
publish PDUFA calendars, and curated databases (BioPharma Catalyst, etc.) are
paid + scraping-hostile. Companies, however, are required to disclose PDUFA
goal dates promptly via 8-K when the FDA assigns one. This fetcher mines that
disclosure stream.

Query strategy: AND-of-required phrases on EDGAR FTS. We pick high-precision
phrasings that biotech IR teams almost always use verbatim:

  "PDUFA goal date"        — canonical
  "PDUFA action date"      — alternate phrasing post-2023
  "PDUFA target action"    — occasional variant

Filter on the EDGAR side, then verify text presence on the client side once
we fetch the full document. We do NOT extract the actual PDUFA date here —
that goes to the agent_reviews medical specialist (which has the openfda + RAG
tools to confirm). event_date is left NULL; the operator dashboard surfaces
NULL-date pending events for triage.

Lookup-only asset resolution: events emitted only when the filer's CIK or
ticker matches a row already in `fda_assets`. Unresolved hits → counted as
`skipped_no_asset`. No fda_assets auto-creation (same reasoning as
fed_register_adcom: silent asset creation pollutes the v3 watchlist).

Idempotency: source_content_hash = sha256("edgar_8k_pdufa:<accession>:<cik>").
fda_regulatory_events UNIQUE (asset_id, event_type, event_date, source_content_hash)
makes ON CONFLICT a no-op for the same filing.

The AFTER INSERT trigger fans pending pdufa events into 3 fda_agent_reviews
rows (medical / regulatory / microstructure) automatically.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... SEC_USER_AGENT="..." \\
    python3 -m modal_workers.fetchers.universe.edgar_8k_pdufa \\
        --start-date 2026-04-01 --end-date 2026-05-11 --apply
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger("edgar_8k_pdufa")

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SOURCE_FEED = "edgar_8k_pdufa"
EVENT_TYPE = "pdufa"
EVENT_STATUS = "pending"
PAGE_SIZE = 100
EDGAR_POLITE_SLEEP_S = 0.12   # 10 req/sec ceiling

# SEC EFTS occasionally returns 5xx under load (observed 2026-05-19 for
# "PDUFA action date" and "PDUFA target action" within the same daily run
# where "PDUFA goal date" succeeded). Without retry the fetcher silently
# drops that query for the day; the same three queries returned HTTP 200
# on 2026-05-20.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_EFTS_RETRIES = 3

# High-precision PDUFA phrasings. Quoted, AND-combined. Each query yields a
# separate page-paged result; we dedupe on accession across queries.
_PDUFA_QUERIES = (
    '"PDUFA goal date"',
    '"PDUFA action date"',
    '"PDUFA target action"',
)


def fetch(
    client: SupabaseClient,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch 8-K filings with a PDUFA-date phrase in [start_date, end_date];
    INSERT pending fda_regulatory_events for resolvable CIK/ticker.

    Multiple PDUFA queries are issued sequentially with per-accession dedup so
    a filing matching both "goal date" AND "action date" produces exactly one
    event.
    """
    fetched = 0
    upserted = 0
    duplicate_accession = 0
    skipped_no_filing_date = 0
    skipped_no_cik = 0
    skipped_no_asset = 0
    errors: List[Dict[str, Any]] = []
    seen_accessions: set[str] = set()
    # Track per-query outcomes so we can distinguish "EDGAR returned nothing"
    # (legitimate quiet period) from "2 of 3 queries silently dropped after
    # retries exhausted". Without this counter the run envelope reports
    # status='ok' even when most of the query plan failed (observed 2026-05-19).
    query_failed: Dict[str, str] = {}
    query_succeeded: List[str] = []

    session = _session()
    for q in _PDUFA_QUERIES:
        offset = 0
        first_page_failed = (offset == 0)
        query_had_error = False
        while True:
            params = {
                "q": q,
                "forms": "8-K",
                "dateRange": "custom",
                "startdt": start_date.isoformat(),
                "enddt": end_date.isoformat(),
                "from": str(offset),
            }
            body, err = _efts_get_with_retry(session, params)
            if err is not None:
                errors.append({"query": q, "offset": offset, "error": err[:400]})
                # Only count as a fully-failed query if the FIRST page errored;
                # mid-pagination failures still landed earlier pages.
                if first_page_failed:
                    query_failed[q] = err[:200]
                query_had_error = True
                break
            first_page_failed = False

            hits = (body.get("hits") or {}).get("hits") or []
            if not hits:
                break

            for hit in hits:
                fetched += 1
                row = _map_hit_to_row(hit)
                if row is None:
                    skipped_no_filing_date += 1
                    continue
                accession = row["accession"]
                if accession in seen_accessions:
                    duplicate_accession += 1
                    continue
                seen_accessions.add(accession)
                cik = row["cik"]
                if not cik:
                    skipped_no_cik += 1
                    continue
                if dry_run:
                    upserted += 1
                    continue
                try:
                    asset_id = _resolve_asset_id(client, cik, row["ticker"])
                except SupabaseError as e:
                    errors.append({
                        "accession": accession,
                        "cik": cik,
                        "error": f"asset lookup: {str(e)[:200]}",
                    })
                    continue
                if asset_id is None:
                    skipped_no_asset += 1
                    continue
                try:
                    if _insert_event(client, row, asset_id):
                        upserted += 1
                except (SupabaseError, ValueError) as e:
                    errors.append({
                        "accession": accession,
                        "cik": cik,
                        "error": str(e)[:400],
                    })

            offset += PAGE_SIZE
            total = (body.get("hits") or {}).get("total", {}).get("value", 0)
            if offset >= int(total or 0):
                break
            time.sleep(EDGAR_POLITE_SLEEP_S)

        if not query_had_error:
            query_succeeded.append(q)

    partial_query_failures = len(query_failed)
    if partial_query_failures:
        logger.warning(
            "edgar_8k_pdufa: %d of %d queries failed after retries; "
            "succeeded=%s failed=%s",
            partial_query_failures, len(_PDUFA_QUERIES),
            query_succeeded, list(query_failed.keys()),
        )

    return {
        "fetched": fetched,
        "upserted": upserted,
        "duplicate_accession": duplicate_accession,
        "skipped_no_filing_date": skipped_no_filing_date,
        "skipped_no_cik": skipped_no_cik,
        "skipped_no_asset": skipped_no_asset,
        "errors": errors,
        "partial_query_failures": partial_query_failures,
        "queries_total": len(_PDUFA_QUERIES),
        "queries_failed": list(query_failed.keys()),
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }


# ---------------------------------------------------------------------------
# EDGAR hit → row
# ---------------------------------------------------------------------------

def _map_hit_to_row(hit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """EDGAR FTS hit → row dict, or None if essential fields are missing."""
    src = hit.get("_source") or {}
    file_date = src.get("file_date")
    if not file_date:
        return None
    try:
        date.fromisoformat(file_date)
    except ValueError:
        return None
    display_names = src.get("display_names") or []
    first_name = display_names[0] if display_names else ""
    cik = _extract_cik(first_name)
    ticker = _extract_ticker(first_name)
    company_name = first_name.split(" (CIK")[0].strip() if first_name else None
    adsh = src.get("adsh") or ""
    accession_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{adsh.replace('-', '')}/{adsh}-index.htm"
        if cik and adsh else None
    )
    return {
        "accession": adsh,
        "cik": cik,
        "ticker": ticker,
        "company_name": company_name,
        "file_date": file_date,
        "source_url": accession_url,
        "display_names": display_names,
    }


def _extract_ticker(display_name: str) -> Optional[str]:
    if not display_name or "(CIK" not in display_name:
        return None
    pre_cik = display_name.split("(CIK")[0]
    open_idx = pre_cik.rfind("(")
    close_idx = pre_cik.rfind(")")
    if open_idx < 0 or close_idx < 0 or close_idx < open_idx:
        return None
    cluster = pre_cik[open_idx + 1:close_idx].strip()
    if not cluster:
        return None
    first = cluster.split(",")[0].strip()
    if not first or len(first) > 10 or first.isdigit():
        return None
    return first


def _extract_cik(display_name: str) -> Optional[str]:
    if "(CIK" not in display_name:
        return None
    try:
        after = display_name.split("(CIK")[1]
        digits = "".join(ch for ch in after.split(")")[0] if ch.isdigit())
        return digits.lstrip("0") or None
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# fda_assets resolution (CIK → entity_identifiers → fda_assets.entity_id;
# fallback ticker → fda_assets.ticker)
# ---------------------------------------------------------------------------

_CIK_ASSET_CACHE: Dict[str, Optional[str]] = {}
_TICKER_ASSET_CACHE: Dict[str, Optional[str]] = {}


def _resolve_asset_id(
    client: SupabaseClient, cik: str, ticker: Optional[str]
) -> Optional[str]:
    """Two-step asset resolution. Returns None when the filer is not in
    the curated fda_assets set — events are not emitted in that case.
    """
    if cik in _CIK_ASSET_CACHE:
        cached = _CIK_ASSET_CACHE[cik]
        if cached is not None:
            return cached
    else:
        try:
            ident_rows = client._rest(
                "GET", "entity_identifiers",
                params={
                    "id_type": "eq.cik", "id_value": f"eq.{cik}",
                    "select": "entity_id", "limit": "1",
                },
            )
        except SupabaseError:
            _CIK_ASSET_CACHE[cik] = None
            ident_rows = []
        if ident_rows:
            entity_id = ident_rows[0]["entity_id"]
            try:
                asset_rows = client._rest(
                    "GET", "fda_assets",
                    params={"entity_id": f"eq.{entity_id}", "select": "id", "limit": "1"},
                )
            except SupabaseError:
                _CIK_ASSET_CACHE[cik] = None
                asset_rows = []
            if asset_rows:
                aid = asset_rows[0]["id"]
                _CIK_ASSET_CACHE[cik] = aid
                return aid
        _CIK_ASSET_CACHE[cik] = None

    if ticker:
        if ticker in _TICKER_ASSET_CACHE:
            return _TICKER_ASSET_CACHE[ticker]
        try:
            rows = client._rest(
                "GET", "fda_assets",
                params={"ticker": f"eq.{ticker}", "select": "id", "limit": "1"},
            )
        except SupabaseError:
            _TICKER_ASSET_CACHE[ticker] = None
            return None
        hit = rows[0]["id"] if rows else None
        _TICKER_ASSET_CACHE[ticker] = hit
        return hit
    return None


# ---------------------------------------------------------------------------
# fda_regulatory_events INSERT (idempotent)
# ---------------------------------------------------------------------------

def _content_hash(accession: str, cik: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"edgar_8k_pdufa:{accession}:{cik}".encode("utf-8")
    ).hexdigest()


def _insert_event(
    client: SupabaseClient, row: Dict[str, Any], asset_id: str
) -> bool:
    """INSERT fda_regulatory_events. Returns True iff a row was newly inserted."""
    accession = row["accession"]
    cik = row["cik"]
    if not accession or not cik:
        return False
    body = [{
        "asset_id": asset_id,
        "event_type": EVENT_TYPE,
        "event_date": None,  # date TBD — specialists fill via agent_reviews
        "event_status": EVENT_STATUS,
        "source_content_hash": _content_hash(accession, cik),
        "notes": f"PDUFA-date 8-K disclosure; date refinement deferred to specialist review.",
        "extensions": {
            "source_feed": SOURCE_FEED,
            "edgar_accession": accession,
            "edgar_cik": cik,
            "edgar_ticker": row.get("ticker"),
            "edgar_file_date": row.get("file_date"),
            "edgar_company_name": row.get("company_name"),
            "edgar_source_url": row.get("source_url"),
        },
    }]
    rows = client._rest_with_retry(
        "POST",
        ("fda_regulatory_events?on_conflict="
         "asset_id,event_type,event_date,source_content_hash"),
        json_body=body,
        prefer="resolution=ignore-duplicates,return=representation",
    )
    if not isinstance(rows, list):
        raise SupabaseError(500, f"unexpected fda_regulatory_events response: {rows!r}")
    return len(rows) > 0


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT env var required (Modal scanner-secrets). "
            'Format: "Name contact@example.com"'
        )
    s.headers.update({"User-Agent": ua, "Accept": "application/json"})
    return s


def _efts_get_with_retry(
    session: requests.Session, params: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Issue one EFTS GET, retrying on 429/5xx and network errors.

    Returns (body, None) on success or (None, error_message) once retries are
    exhausted. The caller appends the error to its accumulator and breaks the
    pagination loop, matching the original bare-GET outer-loop semantics.
    """
    last_err = "no attempt made"
    for attempt in range(_MAX_EFTS_RETRIES):
        try:
            r = session.get(EDGAR_SEARCH_URL, params=params, timeout=30)
            status = getattr(r, "status_code", None)
            if status in _RETRYABLE_STATUS_CODES:
                last_err = f"HTTP {status} (retryable)"
                if attempt + 1 < _MAX_EFTS_RETRIES:
                    time.sleep(min(4.0, 0.6 * (2 ** attempt)) + 0.05)
                    continue
                return None, last_err
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.RequestException as exc:
            last_err = str(exc)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retriable = (
                status in _RETRYABLE_STATUS_CODES
                or isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))
            )
            if retriable and attempt + 1 < _MAX_EFTS_RETRIES:
                time.sleep(min(4.0, 0.6 * (2 ** attempt)) + 0.05)
                continue
            return None, last_err
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
    return None, last_err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=14)).isoformat())
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="Write to Supabase. Default dry-run.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(client, start_date=start, end_date=end, dry_run=not args.apply)
    for k in (
        "window", "fetched", "upserted",
        "duplicate_accession", "skipped_no_filing_date",
        "skipped_no_cik", "skipped_no_asset",
    ):
        print(f"{k}: {result[k]}")
    if result["errors"]:
        print(f"errors: {len(result['errors'])}")
        for err in result["errors"][:5]:
            print(f"  - {err}")
    return 0


class _DryClient:
    def _rest(self, *a, **kw):
        return []

    def _rest_with_retry(self, *a, **kw):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
