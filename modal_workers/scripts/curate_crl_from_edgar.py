"""Curate CRL eval_harness rows from EDGAR 8-K filings.

Phase 0 close-out — D1. The openFDA-driven curation produced only 1 CRL out of
37 rows; openFDA /drug/drugsfda underreports CRLs (sponsors often re-file as
new submissions, masking the original RL status). 8-K disclosures are the most
reliable public CRL source: biotech sponsors disclose receipt of a Complete
Response Letter under Item 8.01 (Other Events) or Item 7.01 (Reg FD).

What this DOES:
  - Search EFTS for forms=8-K, query="complete response letter", chunked by
    quarter across 2023-2024 to stay under EFTS size caps.
  - For each hit, fetch the filing text and regex-extract drug name +
    NDA/BLA application number.
  - Resolve CIK -> ticker via the entities table.
  - Upsert fda_assets row.
  - Insert eval_harness row with realized_outcome='crl', reference_assessment_date
    = filing_date - 30d, realized_outcome_data.source='edgar_8k'.

What this DOES NOT:
  - Polygon move backfill (D2 handles).
  - Document set backfill (D4 handles).
  - Indication backfill (D3 handles).

Run:
  python3 -m modal_workers.scripts.curate_crl_from_edgar \\
      --since 2023-01-01 --until 2024-12-31 [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from modal_workers.shared.edgar_efts import efts_search, fetch_filing_text
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


CRL_QUERY = '"complete response letter"'
CRL_FORMS = "8-K"
EFTS_PAGE_SIZE = 100  # EFTS max per request


@dataclass
class CRLHit:
    """One 8-K disclosure that mentions a CRL."""
    cik: str
    adsh: str
    file_id: str
    file_date: str            # YYYY-MM-DD
    raw_text: str
    drug_brand: Optional[str]
    application_number: Optional[str]   # NDA-XXXXXX / BLA-XXXXXX form when extractable
    extension_meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Stats:
    hits_seen: int = 0
    hits_no_text: int = 0
    hits_no_drug_extracted: int = 0
    hits_no_ticker_match: int = 0
    fda_assets_inserted: int = 0
    eval_harness_inserted: int = 0
    eval_harness_dedup_skipped: int = 0
    errors: int = 0


# ---------------------------------------------------------------------------
# Drug name + application_number extraction
# ---------------------------------------------------------------------------

# Application number patterns. SEC filings commonly write:
#   "NDA No. 213947", "NDA #213947", "NDA 213947"
#   "BLA No. 761234", "Biologics License Application 761234"
_APP_NUM_PATTERNS = [
    re.compile(r"\b(?:NDA|BLA)\s*(?:No\.?|Number|#)?\s*(\d{6})\b", re.IGNORECASE),
    re.compile(r"\b(?:application\s+(?:no\.?|number))\s*(\d{6})\b", re.IGNORECASE),
    re.compile(r"\b(?:NDA|BLA)-(\d{6})\b", re.IGNORECASE),
]

# Locate the CRL phrase (case-insensitive); drug name is then found
# case-sensitively in a window around the match.
_CRL_PHRASE = re.compile(r"complete\s+response\s+letter", re.IGNORECASE)

# Drug name shape (case-sensitive). Either TitleCase ≥4 chars (e.g. "Tarprevin")
# or ALLCAPS 4–15 chars (e.g. "REVLIMID", "OZEMPIC"). Excludes ALLCAPS >15 chars
# (those tend to be acronyms like "ANNOUNCEMENT", "PHARMACEUTICALS").
_DRUG_TOKEN = re.compile(r"\b([A-Z][a-z][A-Za-z0-9\-]{2,23}|[A-Z]{4,15})\b")

# Stop words and false positives that the drug shape can match (TitleCase
# nouns, ALLCAPS section headers, sponsor-name fragments, etc.).
_DRUG_FALSE_POSITIVES = {
    "company", "corporation", "incorporated", "limited", "holdings", "the",
    "this", "that", "these", "those", "their", "today", "yesterday",
    "complete", "response", "letter", "letters",
    "received", "application", "approval", "approved", "fda",
    "biotech", "pharma", "pharmaceutical", "pharmaceuticals", "therapeutics",
    "biosciences", "biopharma", "sciences", "biopharmaceutical",
    "investigational", "study", "studies", "clinical", "phase",
    "drug", "drugs", "product", "products", "candidate", "candidates",
    "regarding", "issued", "agency", "review", "submission",
    "announcement", "announce", "announced", "shareholders",
    "investors", "filing", "reports", "annual", "quarterly", "fiscal",
    "press", "release", "form", "exchange",
    "securities", "commission", "regulation", "regulatory", "rules",
    # Connectives / prepositions
    "from", "for", "with", "without", "into", "onto", "about", "against",
    "between", "during", "regarding", "concerning", "including", "including:",
    "before", "after", "since", "until", "through", "across", "until",
    "above", "below", "under", "over",
    # Pronouns / articles
    "their", "there", "where", "which", "while", "whose", "whose",
    # Months and weekdays
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    # Common biotech jargon misfires
    "nme", "biologic", "biological", "advisory", "committee",
    "guidance", "guidelines", "policy", "policies",
    # SEC filing structural words
    "item", "section", "exhibit", "schedule", "amendment", "registrant",
    "ticker", "symbol", "registered", "trademark", "trademarks",
    # Common ALLCAPS in 8-Ks
    "FDA", "NDA", "BLA", "ANDA", "CRL", "PDUFA", "EOP", "FAQ",
    "SEC", "GAAP", "FORM", "ITEM", "PART", "CFR",
}


def _scan_drug_in_window(window: str) -> Optional[str]:
    """Return the first plausible drug-name token in `window`, or None."""
    for m in _DRUG_TOKEN.finditer(window):
        cand = m.group(1).strip()
        if cand.lower() in _DRUG_FALSE_POSITIVES:
            continue
        return cand
    return None


def extract_drug_and_app(raw_text: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort extraction of (drug_brand, application_number)."""
    text = raw_text[:50000]  # cap; first 50k chars covers Item 8.01 body

    application_number: Optional[str] = None
    for p in _APP_NUM_PATTERNS:
        m = p.search(text)
        if m:
            application_number = m.group(1)
            break

    drug_brand: Optional[str] = None
    for crl_match in _CRL_PHRASE.finditer(text):
        # Try a forward window (drug typically appears after "...for").
        forward = text[crl_match.end(): crl_match.end() + 300]
        cand = _scan_drug_in_window(forward)
        if cand:
            drug_brand = cand
            break
        # Fall back to backward window — some 8-Ks lead with the drug name.
        start = max(0, crl_match.start() - 300)
        backward = text[start: crl_match.start()]
        # Walk backward tokens (last-mentioned drug-shaped token wins)
        all_in_back = list(_DRUG_TOKEN.finditer(backward))
        for bm in reversed(all_in_back):
            c = bm.group(1).strip()
            if c.lower() in _DRUG_FALSE_POSITIVES:
                continue
            drug_brand = c
            break
        if drug_brand:
            break

    return drug_brand, application_number


# ---------------------------------------------------------------------------
# CIK -> ticker resolution
# ---------------------------------------------------------------------------

def resolve_cik_to_entity(cik: str, client: SupabaseClient) -> Optional[Dict[str, Any]]:
    """Look up an entity by CIK. Returns entity row with ticker, or None."""
    cik_padded = cik.zfill(10)
    cik_unpadded = cik.lstrip("0") or "0"

    # Two lookup paths: direct entities.cik column (if present) and the
    # entity_identifiers junction table (canonical fallback chain).
    for cik_value in (cik_padded, cik_unpadded):
        try:
            ident = client._rest(
                "GET", "entity_identifiers",
                params={
                    "id_type": "eq.cik",
                    "id_value": f"eq.{cik_value}",
                    "select": "entity_id",
                    "limit": "1",
                },
            ) or []
        except Exception:  # noqa: BLE001
            ident = []
        if ident:
            entity_id = ident[0]["entity_id"]
            rows = client._rest(
                "GET", "entities",
                params={
                    "id": f"eq.{entity_id}",
                    "select": "id,name,primary_ticker,primary_mic,issuer_figi",
                    "limit": "1",
                },
            ) or []
            if rows and rows[0].get("primary_ticker"):
                return rows[0]

    # Some entities have CIK directly on the row (older convention).
    try:
        rows = client._rest(
            "GET", "entities",
            params={
                "cik": f"eq.{cik_unpadded}",
                "select": "id,name,primary_ticker,primary_mic,issuer_figi",
                "primary_ticker": "not.is.null",
                "limit": "1",
            },
        ) or []
    except Exception:  # noqa: BLE001
        rows = []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# fda_assets upsert + eval_harness insert (CRL-specific)
# ---------------------------------------------------------------------------

def upsert_fda_asset_for_crl(
    *,
    ticker: str,
    mic: Optional[str],
    entity_id: str,
    drug_brand: str,
    application_number: Optional[str],
    sponsor_name: Optional[str],
    client: SupabaseClient,
) -> Optional[str]:
    """Find or create the fda_assets row for a CRL hit. Returns asset_id."""
    natural_app = application_number or f"8K_DERIVED_{drug_brand}"

    existing = client._rest(
        "GET", "fda_assets",
        params={
            "select": "id",
            "ticker": f"eq.{ticker}",
            "drug_name": f"eq.{drug_brand}",
            "limit": "1",
        },
    ) or []
    if existing:
        return existing[0]["id"]

    row = {
        "ticker": ticker,
        "mic": mic,
        "entity_id": entity_id,
        "drug_name": drug_brand,
        "generic_name": None,
        "application_number": natural_app,
        "application_type": "BLA" if natural_app.startswith("76") else "NDA",
        "indication": None,
        "sponsor_name": sponsor_name,
        "is_active": False,
        "watch_priority": 4,
        "extensions": {
            "source": "edgar_8k_crl",
            "curated_for_eval_harness": True,
            "application_number_extracted": application_number,
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
                         ticker, drug_brand, exc)
        return None
    if not rows:
        return None
    return rows[0]["id"]


def insert_crl_eval_harness_row(
    *,
    asset_id: str,
    hit: CRLHit,
    sponsor_name: Optional[str],
    client: SupabaseClient,
) -> str:
    """Insert an eval_harness row for a CRL hit. Returns one of
    'inserted', 'dedup', 'error'."""
    try:
        filing_date = datetime.strptime(hit.file_date, "%Y-%m-%d").date()
    except ValueError:
        return "error"
    reference_date = filing_date - timedelta(days=30)

    natural_app = hit.application_number or f"8K_DERIVED_{hit.adsh}"
    realized_outcome_data = {
        "source": "edgar_8k",
        "curated_by": "curate_crl_from_edgar_v0.1",
        "application_number": natural_app,
        "application_number_extracted": hit.application_number,
        "submission_status": "RL",
        "submission_class_code": None,
        "approval_or_crl_date": filing_date.isoformat(),
        "drug_brand": hit.drug_brand,
        "drug_generic": None,
        "sponsor_name": sponsor_name,
        "edgar_cik": hit.cik,
        "edgar_adsh": hit.adsh,
        "realized_move_pct": None,
    }

    existing = client._rest(
        "GET", "eval_harness",
        params={
            "select": "id",
            "asset_id": f"eq.{asset_id}",
            "realized_outcome_data->>edgar_adsh": f"eq.{hit.adsh}",
            "limit": "1",
        },
    ) or []
    if existing:
        return "dedup"

    row = {
        "asset_id": asset_id,
        "reference_assessment_date": reference_date.isoformat(),
        "realized_outcome": "crl",
        "realized_outcome_data": realized_outcome_data,
        "document_set": [],
        "is_holdout": True,
        "difficulty": "medium",
        "notes": (
            f"Curated from EDGAR 8-K {hit.cik}:{hit.adsh} dated "
            f"{hit.file_date}. CRL disclosure mined from filing body. "
            f"Document set empty until D4 backfill."
        ),
    }
    try:
        client._rest(
            "POST", "eval_harness",
            json_body=row,
            prefer="return=minimal",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("eval_harness insert failed for asset %s adsh %s: %s",
                       asset_id, hit.adsh, exc)
        return "error"
    return "inserted"


# ---------------------------------------------------------------------------
# EFTS sweep, chunked by quarter
# ---------------------------------------------------------------------------

def quarter_windows(since: date, until: date) -> List[Tuple[date, date]]:
    """Walk [since, until] in quarter-sized windows."""
    out: List[Tuple[date, date]] = []
    cur = since.replace(day=1)
    while cur <= until:
        # End of this quarter (3 calendar months out, last day of that month).
        q_end_month = cur.month + 2
        q_end_year = cur.year + (q_end_month - 1) // 12
        q_end_month = ((q_end_month - 1) % 12) + 1
        # Last day of q_end_month
        if q_end_month == 12:
            next_first = date(q_end_year + 1, 1, 1)
        else:
            next_first = date(q_end_year, q_end_month + 1, 1)
        q_end = min(next_first - timedelta(days=1), until)
        out.append((cur, q_end))
        # Advance to start of next quarter
        cur = next_first
    return out


def sweep_crl_8ks(
    *,
    since: date,
    until: date,
    user_agent: str,
    max_per_window: int = EFTS_PAGE_SIZE,
) -> List[CRLHit]:
    """Pull 8-Ks containing the CRL phrase across quarterly windows."""
    hits: List[CRLHit] = []
    for q_start, q_end in quarter_windows(since, until):
        logger.info("EFTS sweep window %s..%s", q_start, q_end)
        raw = efts_search(
            CRL_QUERY, q_start.isoformat(), q_end.isoformat(),
            forms=CRL_FORMS, size=max_per_window, user_agent=user_agent,
        )
        if not raw:
            logger.info("  -> 0 hits")
            continue
        if len(raw) >= max_per_window:
            logger.warning(
                "  -> %d hits (window saturated EFTS page size; some hits may "
                "be missing — narrow the window if precision matters)",
                len(raw),
            )
        else:
            logger.info("  -> %d hits", len(raw))

        for h in raw:
            file_id = h.get("_id")
            src = h.get("_source") or {}
            ciks = src.get("ciks") or []
            cik = ciks[0] if ciks else None
            adsh = src.get("adsh")
            file_date = src.get("file_date")
            if not (file_id and cik and adsh and file_date):
                continue
            text = fetch_filing_text(file_id, cik, adsh, user_agent=user_agent)
            if not text:
                hits.append(CRLHit(
                    cik=cik, adsh=adsh, file_id=file_id, file_date=file_date,
                    raw_text="", drug_brand=None, application_number=None,
                    extension_meta={"display_names": src.get("display_names") or [],
                                    "tickers": src.get("tickers") or []},
                ))
                continue
            drug_brand, app_number = extract_drug_and_app(text)
            hits.append(CRLHit(
                cik=cik, adsh=adsh, file_id=file_id, file_date=file_date,
                raw_text=text, drug_brand=drug_brand,
                application_number=app_number,
                extension_meta={"display_names": src.get("display_names") or [],
                                "tickers": src.get("tickers") or []},
            ))
    return hits


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="curate_crl_from_edgar")
    p.add_argument("--since", default="2023-01-01", help="YYYY-MM-DD inclusive")
    p.add_argument("--until", default="2024-12-31", help="YYYY-MM-DD inclusive")
    p.add_argument("--dry-run", action="store_true",
                   help="Sweep + extract but don't insert")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    until = datetime.strptime(args.until, "%Y-%m-%d").date()
    user_agent = os.environ.get(
        "SEC_USER_AGENT",
        "Conan/1.0 (FDA orchestrator; https://github.com/marazuela/conan)",
    )

    client = SupabaseClient()
    stats = Stats()
    skipped_for_review: List[Dict[str, Any]] = []

    logger.info("Sweeping EDGAR 8-Ks %s..%s for CRL disclosures", since, until)
    hits = sweep_crl_8ks(since=since, until=until, user_agent=user_agent)
    stats.hits_seen = len(hits)
    logger.info("Pulled %d 8-K hits across all windows", len(hits))

    for h in hits:
        if not h.raw_text:
            stats.hits_no_text += 1
            continue
        if not h.drug_brand:
            stats.hits_no_drug_extracted += 1
            skipped_for_review.append({
                "cik": h.cik, "adsh": h.adsh, "file_date": h.file_date,
                "reason": "no drug name extracted",
                "snippet": h.raw_text[:200],
            })
            continue

        entity = resolve_cik_to_entity(h.cik, client)
        if not entity:
            stats.hits_no_ticker_match += 1
            skipped_for_review.append({
                "cik": h.cik, "adsh": h.adsh, "file_date": h.file_date,
                "drug": h.drug_brand,
                "reason": "no entity row with primary_ticker for CIK",
            })
            continue

        ticker = entity.get("primary_ticker")
        if not ticker:
            stats.hits_no_ticker_match += 1
            continue

        sponsor_name = (h.extension_meta.get("display_names") or [None])[0] \
            or entity.get("name")

        if args.dry_run:
            logger.info(
                "[dry-run] %s %s %s -> ticker=%s drug=%s app=%s",
                h.file_date, h.cik, h.adsh, ticker, h.drug_brand,
                h.application_number,
            )
            continue

        asset_id = upsert_fda_asset_for_crl(
            ticker=ticker, mic=entity.get("primary_mic"),
            entity_id=entity["id"], drug_brand=h.drug_brand,
            application_number=h.application_number,
            sponsor_name=sponsor_name, client=client,
        )
        if not asset_id:
            stats.errors += 1
            continue

        outcome = insert_crl_eval_harness_row(
            asset_id=asset_id, hit=h, sponsor_name=sponsor_name, client=client,
        )
        if outcome == "inserted":
            stats.eval_harness_inserted += 1
        elif outcome == "dedup":
            stats.eval_harness_dedup_skipped += 1
        else:
            stats.errors += 1

    logger.info(
        "CRL curation summary: hits=%d no_text=%d no_drug=%d no_ticker=%d "
        "inserted=%d dedup_skipped=%d errors=%d",
        stats.hits_seen, stats.hits_no_text, stats.hits_no_drug_extracted,
        stats.hits_no_ticker_match, stats.eval_harness_inserted,
        stats.eval_harness_dedup_skipped, stats.errors,
    )

    if skipped_for_review:
        logger.info("First 30 skipped hits (operator review):")
        for s in skipped_for_review[:30]:
            print(f"  {s.get('file_date')} cik={s.get('cik')} "
                  f"adsh={s.get('adsh')} drug={s.get('drug', '?')} "
                  f"reason={s.get('reason')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
