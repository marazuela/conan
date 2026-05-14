"""One-shot seeder: ingest documents for fda_assets that the orchestrator
cannot reach because the `public.documents` table has nothing matching their
drug.

The companion script `backfill_v3_assessments.py` revealed that for ~25 of
the active assets missing a convergence_assessment, the documents table
holds zero rows that title-match the asset's drug name. The fda_event_evidence
table cites real press-release/journal/FDA URLs for those assets but none of
them have been ingested. This script attacks that upstream gap directly.

For each active fda_asset that:
  - lacks a convergence_assessment,
  - lacks a primary+material asset_documents link,
  - has a non-garbage drug_name (length ≥ MIN_QUERY_LEN, not in known-bad list),
this script invokes the four primary ingestion adapters with the asset's
drug_name (and application_number where present). Each adapter writes to
`public.documents` via the shared `DocumentWriter`, which dedups via
`UNIQUE(source, source_content_hash)`. Reruns are idempotent.

Adapters used:
  - clinicaltrials.ingest_search(drug_name)
  - openfda.ingest_drugsfda_approvals(application_search=appl#) — if numeric
  - federal_register.ingest_keyword_search(drug_name)
  - edgar.ingest_keyword_search(drug_name, forms="8-K,10-K,10-Q,S-1") — opt-in

clinicaltrials is the most valuable: the asset_linker pass-1 source allowlist
is currently `('clinicaltrials',)` (see modal_workers/extractor/asset_linker.py
SOURCE_ALLOWLIST), so newly-ingested clinicaltrials docs will get classified
into asset_documents automatically when the linker next runs. For the other
sources, the title-match heuristic in backfill_v3_assessments.py will now
find a match where it didn't before, even before the linker catches up.

EDGAR is opt-in (--with-edgar) because:
  - It requires SEC_USER_AGENT to be set,
  - The keyword search for short drug-name tokens returns lots of unrelated
    sponsor 8-Ks, and
  - The asset_linker's source allowlist excludes EDGAR right now (a
    2026-05-13 gold-set eval saw 0% pass-1 yield from EDGAR — see the
    SOURCE_ALLOWLIST comment in asset_linker.py).

Run:
  SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… \\
    python3 -m modal_workers.scripts.seed_documents_for_missing_assets \\
        [--dry-run] [--limit N] [--with-edgar] [--lookback-days 365]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from modal_workers.ingestion import (
    clinicaltrials_ingest,
    edgar_ingest,
    federal_register_ingest,
    openfda_ingest,
)
from modal_workers.shared.document_writer import DocumentWriter
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

# Minimum chars in the search query. The clinicaltrials.gov `query.term`
# endpoint returns thousands of hits for short generic tokens — keep this
# specific.
MIN_QUERY_LEN = 5

# Known-garbage drug_names (same set as backfill_v3_assessments). EDGAR
# 'EX-99' is an exhibit-file convention, not a drug.
KNOWN_GARBAGE_DRUG_NAMES = {"ex-99", "exhibit 99", "exhibit-99"}

# Tokens that, if used as the query, would dredge up unrelated documents
# (already learned the hard way in backfill_v3_assessments).
GENERIC_STOP_TOKENS = {
    "carbonate", "bicarbonate", "phosphate", "sulfate", "chloride",
    "sodium", "potassium", "calcium", "magnesium", "hydrochloride",
    "formulation", "tablet", "capsule", "solution", "injection",
    "cream", "ointment", "patch", "spray", "gel", "drops",
}

# EDGAR form filter — focus on filings most likely to discuss a drug:
# 8-K (current report), 10-K/Q (annuals/quarterlies), S-1 (registration).
EDGAR_FORMS = "8-K,10-K,10-Q,S-1,424B1,424B2,424B3,424B4,424B5,424B7"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@dataclass
class PerAssetStats:
    asset_id: str
    ticker: str
    drug_name: str
    queries_used: List[str] = field(default_factory=list)
    ct_seen: int = 0
    ct_written: int = 0
    ct_dedup: int = 0
    openfda_seen: int = 0
    openfda_written: int = 0
    openfda_dedup: int = 0
    fr_seen: int = 0
    fr_written: int = 0
    fr_dedup: int = 0
    edgar_seen: int = 0
    edgar_written: int = 0
    edgar_dedup: int = 0
    nct_direct_fetched: int = 0
    sponsor_fallback_used: bool = False
    sponsor_fallback_wrote: int = 0
    errors: int = 0
    skipped_reason: Optional[str] = None


@dataclass
class Stats:
    assets_seen: int = 0
    assets_skipped: int = 0
    assets_ingested: int = 0
    total_documents_written: int = 0
    total_documents_dedup: int = 0
    errors: int = 0
    per_asset: List[PerAssetStats] = field(default_factory=list)


# ---------------------------------------------------------------------------
# query selection
# ---------------------------------------------------------------------------

_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z0-9-]+")


def candidate_queries_for_asset(asset: Dict[str, Any]) -> List[str]:
    """Build the ordered list of search queries to try for one asset.

    Order of preference:
      1. drug_name + generic_name tokens (longest first)
      2. extensions.compound_codes entries (these are the asset's development
         codes; trial registry names often use these instead of the brand)
      3. sponsor_name fallback — broad and noisy but unlocks assets whose drug
         doesn't appear by any token on CT.gov

    Returns [] when nothing usable exists (corrupt drug_name, no sponsor).
    """
    drug_name = asset.get("drug_name") or ""
    if drug_name.strip().lower() in KNOWN_GARBAGE_DRUG_NAMES:
        return []

    seen: Dict[str, None] = {}
    out: List[str] = []

    # 1) tokens from drug_name + generic_name
    name_tokens: List[str] = []
    for src in (drug_name, asset.get("generic_name") or ""):
        for m in _TOKEN_RX.finditer(src):
            tok = m.group(0)
            if len(tok) < MIN_QUERY_LEN:
                continue
            if tok.lower() in GENERIC_STOP_TOKENS:
                continue
            name_tokens.append(tok)
    # longest first (proxy for specificity)
    name_tokens.sort(key=lambda t: (-len(t), t))
    for tok in name_tokens:
        if tok not in seen:
            seen[tok] = None
            out.append(tok)

    # 2) compound_codes from extensions — these are asset-level aliases
    # (e.g. CAP-1002 for Deramiocel, AAV8-G6PC for DTX401).
    ext = asset.get("extensions") or {}
    for code in (ext.get("compound_codes") or []):
        if not isinstance(code, str):
            continue
        code = code.strip()
        # Allow multi-word codes like "anitocabtagene autoleucel"; the CT.gov
        # query.term accepts phrases. Skip ones that are too short to be
        # useful as a query.
        if len(code) < MIN_QUERY_LEN:
            continue
        if code not in seen:
            seen[code] = None
            out.append(code)

    return out


def sponsor_query_for_asset(asset: Dict[str, Any]) -> Optional[str]:
    """Return a sponsor-name query to use as a last-resort fallback.

    Only used when the drug/code query strategy yielded zero new docs.
    Surfaces all of the sponsor's recent trials; the asset_linker downstream
    picks which are actually about the target asset.
    """
    sponsor = (asset.get("sponsor_name") or "").strip()
    if not sponsor:
        ext = asset.get("extensions") or {}
        sponsor = (ext.get("company_name") or "").strip()
    return sponsor or None


def phase3_nctid_for_asset(asset: Dict[str, Any]) -> Optional[str]:
    """Direct NCT id from extensions, if curated. Used by ingest_by_nct
    to fetch the trial registry record without searching."""
    ext = asset.get("extensions") or {}
    nctid = (ext.get("phase3_nctid") or "").strip()
    # Validate shape: NCT followed by 8 digits.
    if re.fullmatch(r"NCT\d{8}", nctid):
        return nctid
    return None


# ---------------------------------------------------------------------------
# asset enumeration
# ---------------------------------------------------------------------------

def find_target_assets(client: SupabaseClient, limit: int) -> List[Dict[str, Any]]:
    """Active fda_assets that lack a convergence_assessment AND lack a
    primary+material asset_documents link.

    The second filter avoids re-ingesting for assets that already have a
    pending orchestrator_run (the prior backfill_v3_assessments run handled
    those).
    """
    active = client._rest(
        "GET",
        "fda_assets",
        params={
            "is_active": "eq.true",
            "select": ("id,ticker,drug_name,generic_name,entity_id,"
                       "application_number,sponsor_name,extensions"),
            "limit": str(limit),
        },
    ) or []
    assessed = client._rest(
        "GET",
        "convergence_assessments",
        params={"select": "asset_id"},
    ) or []
    assessed_ids = {row["asset_id"] for row in assessed if row.get("asset_id")}

    primary_links = client._rest(
        "GET",
        "asset_documents",
        params={
            "link_type": "eq.primary",
            "is_material": "eq.true",
            "select": "asset_id",
        },
    ) or []
    linked_ids = {row["asset_id"] for row in primary_links if row.get("asset_id")}

    targets: List[Dict[str, Any]] = []
    for row in active:
        if row["id"] in assessed_ids:
            continue
        if row["id"] in linked_ids:
            continue
        targets.append(row)
    return targets


# ---------------------------------------------------------------------------
# per-asset ingestion
# ---------------------------------------------------------------------------

def ingest_one_asset(
    asset: Dict[str, Any],
    *,
    writer: DocumentWriter,
    sec_user_agent: str,
    lookback_days: int,
    with_edgar: bool,
    dry_run: bool,
) -> PerAssetStats:
    drug_name = asset.get("drug_name") or ""
    out = PerAssetStats(
        asset_id=asset["id"],
        ticker=asset.get("ticker") or "",
        drug_name=drug_name,
    )

    queries = candidate_queries_for_asset(asset)
    if not queries:
        out.skipped_reason = "no usable query token (garbage drug_name or too short)"
        return out
    out.queries_used = queries[:]  # snapshot before we extend

    if dry_run:
        out.skipped_reason = "dry-run"
        return out

    until = date.today()
    since = until - timedelta(days=lookback_days)

    # 0) Direct NCT fetch — if the asset row has a curated phase3_nctid,
    # pull that trial registry record by id (no search needed). The CT.gov
    # record itself is the canonical primary doc.
    nctid = phase3_nctid_for_asset(asset)
    if nctid:
        try:
            r = clinicaltrials_ingest.ingest_by_nct([nctid], writer=writer)
            out.nct_direct_fetched = r.documents_written
            out.ct_seen += r.documents_seen
            out.ct_written += r.documents_written
            out.ct_dedup += r.documents_dedup_hit
        except Exception as exc:  # noqa: BLE001
            logger.warning("ct ingest_by_nct failed for %s/%s: %s",
                           out.ticker, nctid, exc)
            out.errors += 1

    # 1) ClinicalTrials.gov keyword search — try EACH query (drug_name tokens
    # + compound_codes aliases). asset_linker actively classifies these per
    # SOURCE_ALLOWLIST=('clinicaltrials',).
    for query in queries:
        try:
            r = clinicaltrials_ingest.ingest_search(
                query, page_size=20, max_pages=2, writer=writer,
            )
            out.ct_seen += r.documents_seen
            out.ct_written += r.documents_written
            out.ct_dedup += r.documents_dedup_hit
        except Exception as exc:  # noqa: BLE001
            logger.warning("ct ingest failed for %s/%s: %s",
                           out.ticker, query, exc)
            out.errors += 1

    # 2) openFDA drugsfda — only if the asset has a numeric application_number.
    appl = (asset.get("application_number") or "").strip()
    if appl.isdigit() and appl != "0":
        try:
            r = openfda_ingest.ingest_drugsfda_approvals(
                application_search=appl,
                since=since - timedelta(days=365),  # openFDA filings older than 365d
                until=until,
                page_limit=50,
                max_pages=2,
                writer=writer,
            )
            out.openfda_seen, out.openfda_written, out.openfda_dedup = (
                r.documents_seen, r.documents_written, r.documents_dedup_hit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("openfda ingest failed for %s/appl=%s: %s",
                           out.ticker, appl, exc)
            out.errors += 1

    # 3) Federal Register — FDA advisory committee meetings, etc. Use the
    # primary (first/longest) query token only — federal_register matches are
    # rare and broader queries balloon the result set with non-drug content.
    try:
        r = federal_register_ingest.ingest_keyword_search(
            queries[0], since=since, until=until, max_pages=2, per_page=50,
            writer=writer,
        )
        out.fr_seen, out.fr_written, out.fr_dedup = (
            r.documents_seen, r.documents_written, r.documents_dedup_hit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fed_register ingest failed for %s/%s: %s",
                       out.ticker, queries[0], exc)
        out.errors += 1

    # 4) EDGAR — opt-in. asset_linker doesn't classify EDGAR docs (per
    # SOURCE_ALLOWLIST), but the title-match heuristic in
    # backfill_v3_assessments will pick them up.
    if with_edgar:
        try:
            r = edgar_ingest.ingest_keyword_search(
                f'"{queries[0]}"', since=since, until=until,
                forms=EDGAR_FORMS, user_agent=sec_user_agent,
                size=50, writer=writer,
            )
            out.edgar_seen, out.edgar_written, out.edgar_dedup = (
                r.documents_seen, r.documents_written, r.documents_dedup_hit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("edgar ingest failed for %s/%s: %s",
                           out.ticker, queries[0], exc)
            out.errors += 1

    # 5) Sponsor-name fallback — last resort if NOTHING was written after the
    # drug/code search. Returns every recent CT.gov trial from the sponsor;
    # noisy but lets the asset_linker decide which trials concern this asset.
    total_wrote = (out.ct_written + out.openfda_written
                   + out.fr_written + out.edgar_written)
    if total_wrote == 0:
        sponsor = sponsor_query_for_asset(asset)
        if sponsor:
            out.sponsor_fallback_used = True
            try:
                r = clinicaltrials_ingest.ingest_search(
                    sponsor, page_size=20, max_pages=2, writer=writer,
                )
                out.sponsor_fallback_wrote = r.documents_written
                out.ct_seen += r.documents_seen
                out.ct_written += r.documents_written
                out.ct_dedup += r.documents_dedup_hit
            except Exception as exc:  # noqa: BLE001
                logger.warning("ct sponsor-fallback failed for %s/%s: %s",
                               out.ticker, sponsor, exc)
                out.errors += 1

    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="seed_documents_for_missing_assets")
    p.add_argument("--limit", type=int, default=200,
                   help="Max active assets to enumerate per run")
    p.add_argument("--lookback-days", type=int, default=365,
                   help="Window for federal_register + edgar searches")
    p.add_argument("--with-edgar", action="store_true",
                   help="Also run EDGAR keyword search (requires SEC_USER_AGENT)")
    p.add_argument("--dry-run", action="store_true",
                   help="Enumerate targets but don't call any adapter")
    p.add_argument("--verbose", action="store_true",
                   help="Per-asset DEBUG logging")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = SupabaseClient()
    writer = DocumentWriter()
    stats = Stats()
    sec_ua = os.environ.get(
        "SEC_USER_AGENT",
        "Conan/1.0 (FDA orchestrator backfill; https://github.com/marazuela/conan)",
    )

    targets = find_target_assets(client, limit=args.limit)
    stats.assets_seen = len(targets)
    logger.info("Found %d active assets missing both assessment and primary doc",
                len(targets))

    t0 = time.time()
    for asset in targets:
        per = ingest_one_asset(
            asset,
            writer=writer,
            sec_user_agent=sec_ua,
            lookback_days=args.lookback_days,
            with_edgar=args.with_edgar,
            dry_run=args.dry_run,
        )
        stats.per_asset.append(per)
        if per.skipped_reason:
            stats.assets_skipped += 1
        else:
            stats.assets_ingested += 1
        wrote = (per.ct_written + per.openfda_written
                 + per.fr_written + per.edgar_written)
        dedup = (per.ct_dedup + per.openfda_dedup
                 + per.fr_dedup + per.edgar_dedup)
        stats.total_documents_written += wrote
        stats.total_documents_dedup += dedup
        stats.errors += per.errors
        sponsor_note = (
            f" sponsor_fallback={per.sponsor_fallback_wrote}"
            if per.sponsor_fallback_used else ""
        )
        nct_note = f" nct_direct={per.nct_direct_fetched}" if per.nct_direct_fetched else ""
        logger.info(
            "%s %s queries=%r ct=%d/%d openfda=%d/%d fr=%d/%d edgar=%d/%d"
            "%s%s errors=%d %s",
            per.ticker, per.asset_id[:8], per.queries_used,
            per.ct_written, per.ct_seen,
            per.openfda_written, per.openfda_seen,
            per.fr_written, per.fr_seen,
            per.edgar_written, per.edgar_seen,
            nct_note, sponsor_note,
            per.errors,
            f"SKIPPED:{per.skipped_reason}" if per.skipped_reason else "",
        )
    elapsed = time.time() - t0

    logger.info(
        "seeder summary: assets_seen=%d ingested=%d skipped=%d "
        "docs_written=%d docs_dedup_hit=%d errors=%d elapsed_s=%.1f",
        stats.assets_seen, stats.assets_ingested, stats.assets_skipped,
        stats.total_documents_written, stats.total_documents_dedup,
        stats.errors, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
