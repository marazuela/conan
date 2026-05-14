"""Federal Register FDA Advisory Committee fetcher → fda_regulatory_events.

Source: Federal Register public API
  https://www.federalregister.gov/api/v1/documents.json

Maps each FDA Advisory Committee "Notice of Meeting" document with a
parseable future meeting date to an `fda_regulatory_events` row at
event_type='adcom', event_status='pending'.

Lookup-only ticker resolution: events are emitted only when the resolved
sponsor matches a row already in `fda_assets` (via primary ticker or sponsor
name). Unresolved hits are counted as `skipped_no_asset` and logged in the
return envelope — they do NOT auto-create assets, because Federal Register
notices reference committee + indication far more often than a specific
sponsor + drug, and silent asset creation would pollute the curated set the
v3 orchestrator depends on. Operators add the asset via the watchlist JSON
or the dashboard before the next fetch run will pick the notice up.

Idempotency: `fda_regulatory_events` UNIQUE
(asset_id, event_type, event_date, source_content_hash). source_content_hash
is sha256("fed_register:<document_number>") — stable across runs, so re-INSERT
of the same Federal Register doc is a no-op via ON CONFLICT DO NOTHING.

The AFTER INSERT trigger `enqueue_fda_agent_reviews_on_event_insert_tg` fans
each pending non-resolution event into three queued `fda_agent_reviews` rows
(medical / regulatory / microstructure) automatically.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fed_register_adcom \\
        --start-date 2026-04-01 --end-date 2026-05-11 --apply
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger("fed_register_adcom")

FEDREG_URL = "https://www.federalregister.gov/api/v1/documents.json"
SOURCE_FEED = "fed_register_adcom"
EVENT_TYPE = "adcom"
EVENT_STATUS = "pending"
USER_AGENT = "Conan v3 research@example.com"
REQUEST_TIMEOUT = 30
PAGE_SIZE = 100

# Federal Register's term-based search returns a wide net. We narrow on the
# CLIENT side to documents that look like an actionable AdComm meeting notice.
# Empirically (2026-05-11 smoke run), Federal Register meeting notices very
# commonly include "Establishment of a Public Docket" in the title — that
# phrase refers to the comment docket for the meeting, not committee
# establishment. So we DON'T exclude on "Establishment". Committee renewals,
# withdrawals, and charter notices use different headings and are rejected
# either by INCLUDE (no "notice of meeting") or by EXCLUDE.
_TITLE_INCLUDE = re.compile(r"notice of meeting", re.IGNORECASE)
_TITLE_EXCLUDE = re.compile(
    r"\b(renewal|withdrawal|charter)\b",
    re.IGNORECASE,
)

# Parse "The meeting will be held on July 23, 2026" or "Meeting: July 23, 2026".
# Multi-day meetings ("July 23, 2026 ... July 24, 2026") collapse to the first
# date — the agent_reviews specialists can refine when they read the notice.
_MEETING_DATE_RE = re.compile(
    r"(?:meeting (?:will be )?held on|meeting:?|date:?)\s+"
    r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)

# Some notices reference the sponsor sub-clause as "<Company>; Notice of Meeting"
# or in the abstract as "to discuss <Drug Name> ... developed by <Company>".
# Best-effort regex — when it misses, we fall back to ticker resolution via
# the abstract sniff in _resolve_sponsor_terms.
_SPONSOR_FROM_TITLE_RE = re.compile(
    r"^([A-Z][\w\.,&\- ]{2,60}?)[;,]\s+notice of meeting",
    re.IGNORECASE,
)

# Tokens stripped before sponsor → fda_assets.sponsor_name ilike. Same set the
# pre_phase3_readout_scanner uses, in spirit.
_SPONSOR_NOISE_TOKENS = {
    "inc", "inc.", "incorporated", "llc", "ltd", "ltd.", "limited",
    "plc", "corp", "corp.", "corporation", "company", "co", "co.",
    "ag", "sa", "s.a.", "sas", "se", "nv", "ab",
    "holdings", "holding", "group",
    "pharmaceutical", "pharmaceuticals", "pharma",
    "biosciences", "biotechnology", "biotech", "therapeutics",
}


def fetch(
    client: SupabaseClient,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch FDA AdComm notices in [start_date, end_date]; insert pending
    `fda_regulatory_events(event_type='adcom')` for sponsor-resolvable hits.
    """
    fetched = 0
    upserted = 0
    skipped_not_meeting = 0
    skipped_no_date = 0
    skipped_no_sponsor = 0
    skipped_no_asset = 0
    errors: List[Dict[str, Any]] = []

    session = _session()
    page = 1
    while True:
        try:
            r = session.get(
                FEDREG_URL,
                params={
                    "conditions[agencies][]": "food-and-drug-administration",
                    "conditions[term]": "advisory committee meeting",
                    "conditions[publication_date][gte]": start_date.isoformat(),
                    "conditions[publication_date][lte]": end_date.isoformat(),
                    "per_page": str(PAGE_SIZE),
                    "page": str(page),
                    "fields[]": [
                        "title", "abstract", "publication_date",
                        "document_number", "html_url", "dates",
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001
            errors.append({"page": page, "error": str(e)[:400]})
            break

        results = body.get("results") or []
        if not results:
            break

        for doc in results:
            fetched += 1
            title = doc.get("title") or ""
            if not _is_meeting_notice(title):
                skipped_not_meeting += 1
                continue
            meeting_date = _parse_meeting_date(doc.get("dates") or "")
            if meeting_date is None:
                skipped_no_date += 1
                continue
            sponsor_terms = _sponsor_terms_from_doc(doc)
            if not sponsor_terms:
                skipped_no_sponsor += 1
                continue
            if dry_run:
                upserted += 1
                continue
            try:
                asset_id = _resolve_asset_id(client, sponsor_terms)
            except SupabaseError as e:
                errors.append({
                    "document_number": doc.get("document_number"),
                    "error": f"asset lookup: {str(e)[:200]}",
                })
                continue
            if asset_id is None:
                skipped_no_asset += 1
                continue
            try:
                if _insert_event(client, doc, asset_id, meeting_date):
                    upserted += 1
            except (SupabaseError, ValueError) as e:
                errors.append({
                    "document_number": doc.get("document_number"),
                    "error": str(e)[:400],
                })

        if (body.get("next_page_url") is None) or page >= 50:
            break
        page += 1

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped_not_meeting": skipped_not_meeting,
        "skipped_no_date": skipped_no_date,
        "skipped_no_sponsor": skipped_no_sponsor,
        "skipped_no_asset": skipped_no_asset,
        "errors": errors,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }


# ---------------------------------------------------------------------------
# Title / abstract / dates parsing
# ---------------------------------------------------------------------------

def _is_meeting_notice(title: str) -> bool:
    if not _TITLE_INCLUDE.search(title or ""):
        return False
    if _TITLE_EXCLUDE.search(title or ""):
        return False
    return True


def _parse_meeting_date(dates_text: str) -> Optional[str]:
    """Pull the first 'Month Day, Year' instance out of the dates blurb.

    Federal Register publishes meeting dates in prose; multi-day meetings list
    each day — we take the first as event_date and let the specialists refine.
    """
    if not dates_text:
        return None
    m = _MEETING_DATE_RE.search(dates_text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _sponsor_terms_from_doc(doc: Dict[str, Any]) -> List[str]:
    """Build a small ranked list of candidate sponsor strings from the title +
    abstract. Returned in highest-precision-first order so the asset lookup can
    short-circuit on the first hit.
    """
    candidates: List[str] = []
    title = doc.get("title") or ""
    m = _SPONSOR_FROM_TITLE_RE.match(title.strip())
    if m:
        candidates.append(m.group(1).strip())
    # Abstract often names the sponsor explicitly. Capture the first
    # PascalCase-ish token cluster (2-4 words, leading capital) ≥ 4 chars.
    abstract = doc.get("abstract") or ""
    for token_match in re.finditer(
        r"\b([A-Z][a-zA-Z]{2,}(?:[\s\-][A-Z][a-zA-Z]{2,}){0,3})\b",
        abstract,
    ):
        tok = token_match.group(1).strip()
        if tok.lower() in {"the", "this", "food", "drug", "advisory", "committee", "fda"}:
            continue
        if tok not in candidates:
            candidates.append(tok)
        if len(candidates) >= 6:
            break
    return candidates


def _normalize_sponsor(sponsor: str) -> str:
    """Strip corporate-suffix noise tokens so ilike matches are lenient.

    Also drops single-letter tokens — these are residue from initials inside
    suffixes like "S.A." or "N.V." after period-splitting, and they break
    substring matches against canonical sponsor names in the DB.
    """
    tokens = [t for t in re.split(r"[\s,\.]+", sponsor) if t]
    kept = [
        t for t in tokens
        if len(t) > 1 and t.lower() not in _SPONSOR_NOISE_TOKENS
    ]
    return " ".join(kept).strip()


# ---------------------------------------------------------------------------
# fda_assets lookup (no auto-create)
# ---------------------------------------------------------------------------

_ASSET_CACHE: Dict[str, Optional[str]] = {}


def _resolve_asset_id(client: SupabaseClient, sponsor_terms: List[str]) -> Optional[str]:
    """Return the first fda_assets.id matching any sponsor candidate, or None.

    Match order per candidate:
      1. fda_assets.sponsor_name ilike '%<normalized>%'  (canonical case)
      2. entities.name ilike → entity_id → fda_assets.entity_id  (fallback for
         tickers that share a parent entity)

    Cached per-process by candidate string.
    """
    for raw in sponsor_terms:
        norm = _normalize_sponsor(raw)
        if not norm or len(norm) < 3:
            continue
        cache_key = norm.lower()
        if cache_key in _ASSET_CACHE:
            hit = _ASSET_CACHE[cache_key]
            if hit is not None:
                return hit
            continue
        try:
            rows = client._rest(
                "GET", "fda_assets",
                params={
                    "sponsor_name": f"ilike.%{norm}%",
                    "select": "id", "limit": "1",
                },
            )
        except SupabaseError:
            _ASSET_CACHE[cache_key] = None
            continue
        if rows:
            asset_id = rows[0]["id"]
            _ASSET_CACHE[cache_key] = asset_id
            return asset_id
        # Fallback: entities table → fda_assets by entity_id
        try:
            ent_rows = client._rest(
                "GET", "entities",
                params={"name": f"ilike.%{norm}%", "select": "id", "limit": "1"},
            )
        except SupabaseError:
            _ASSET_CACHE[cache_key] = None
            continue
        if not ent_rows:
            _ASSET_CACHE[cache_key] = None
            continue
        entity_id = ent_rows[0]["id"]
        try:
            asset_rows = client._rest(
                "GET", "fda_assets",
                params={"entity_id": f"eq.{entity_id}", "select": "id", "limit": "1"},
            )
        except SupabaseError:
            _ASSET_CACHE[cache_key] = None
            continue
        if asset_rows:
            asset_id = asset_rows[0]["id"]
            _ASSET_CACHE[cache_key] = asset_id
            return asset_id
        _ASSET_CACHE[cache_key] = None
    return None


# ---------------------------------------------------------------------------
# fda_regulatory_events INSERT (idempotent)
# ---------------------------------------------------------------------------

def _content_hash(document_number: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"fed_register:{document_number}".encode("utf-8")
    ).hexdigest()


def _insert_event(
    client: SupabaseClient,
    doc: Dict[str, Any],
    asset_id: str,
    meeting_date: str,
) -> bool:
    """INSERT fda_regulatory_events. Returns True if a row was newly inserted.

    Idempotent via ON CONFLICT DO NOTHING on
    (asset_id, event_type, event_date, source_content_hash).
    """
    doc_num = doc.get("document_number") or ""
    if not doc_num:
        return False
    body = [{
        "asset_id": asset_id,
        "event_type": EVENT_TYPE,
        "event_date": meeting_date,
        "event_status": EVENT_STATUS,
        "source_content_hash": _content_hash(doc_num),
        "notes": (doc.get("title") or "")[:500] or None,
        "extensions": {
            "source_feed": SOURCE_FEED,
            "fed_register_document_number": doc_num,
            "fed_register_publication_date": doc.get("publication_date"),
            "fed_register_html_url": doc.get("html_url"),
            "fed_register_dates_text": (doc.get("dates") or "")[:500],
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
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


# ---------------------------------------------------------------------------
# CLI (local dry-run / apply)
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=30)).isoformat())
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
        "skipped_not_meeting", "skipped_no_date",
        "skipped_no_sponsor", "skipped_no_asset",
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
