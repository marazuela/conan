"""Hydrate fda_assets designation + sponsor-history columns for the binary-catalyst pre-gate.

For each active fda_asset, write:
  - priority_review            from 8-K extracted_facts (fact_type='designation')
  - breakthrough_designation   from 8-K extracted_facts
  - sponsor_prior_nda_count    openFDA drugsfda count of the sponsor's prior approved apps
  - first_time_sponsor         (sponsor_prior_nda_count == 0) on a confirmed lookup
  - designations_enriched_at   now()

Decoupled from the scanner's per-scan 30-call openFDA budget, which starved these
inputs (populated <2% of signals -> the pre-gate scored every asset 0). The
designation flags come from our own verified filings, not openFDA: pending pre-PDUFA
applications have no application_number in Drugs@FDA, so openFDA can't supply them.

Run:
  python3 -m modal_workers.scripts.enrich_fda_asset_designations [--limit N] [--dry-run]
      [--stale-hours H] [--asset-id UUID]

--stale-hours H skips assets enriched within the last H hours (0 = re-enrich all).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.scripts.backfill_v3_assessments import is_garbage_drug_name
from modal_workers.shared.bc_pregate_inputs import (
    count_sponsor_prior_nda,
    first_time_sponsor,
    parse_designation_flags,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    assets_seen: int = 0
    skipped_garbage: int = 0
    priority_review_true: int = 0
    breakthrough_true: int = 0
    first_time_sponsor_true: int = 0
    sponsor_unknown: int = 0
    updated: int = 0
    errors: int = 0


def _fetch_designation_facts(client: SupabaseClient, asset_id: str) -> List[Dict[str, Any]]:
    return client._rest(
        "GET", "extracted_facts",
        params={
            "select": "fact_text,confidence",
            "asset_id": f"eq.{asset_id}",
            "fact_type": "eq.designation",
        },
    ) or []


def _update_asset(client: SupabaseClient, asset_id: str, payload: Dict[str, Any]) -> bool:
    try:
        client._rest(
            "PATCH", "fda_assets",
            params={"id": f"eq.{asset_id}"},
            json_body=payload,
            prefer="return=minimal",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PATCH fda_assets failed for %s: %s", asset_id, exc)
        return False
    return True


def enrich_asset(client: SupabaseClient, row: Dict[str, Any], stats: Stats,
                 *, dry_run: bool) -> None:
    asset_id = row["id"]
    sponsor_name = (row.get("sponsor_name") or "").strip()

    facts = _fetch_designation_facts(client, asset_id)
    flags = parse_designation_flags(facts)

    prior_nda = count_sponsor_prior_nda(sponsor_name)
    fts = first_time_sponsor(prior_nda)

    if flags["priority_review"]:
        stats.priority_review_true += 1
    if flags["breakthrough_designation"]:
        stats.breakthrough_true += 1
    if fts:
        stats.first_time_sponsor_true += 1
    if prior_nda is None:
        stats.sponsor_unknown += 1

    payload = {
        "priority_review": flags["priority_review"],
        "breakthrough_designation": flags["breakthrough_designation"],
        "sponsor_prior_nda_count": prior_nda,
        "first_time_sponsor": fts,
        "designations_enriched_at": datetime.now(timezone.utc).isoformat(),
    }

    if dry_run:
        logger.info(
            "[dry-run] %s/%s pr=%s bt=%s prior_nda=%s first_time=%s (designation_facts=%d)",
            row.get("ticker"), row.get("drug_name"),
            flags["priority_review"], flags["breakthrough_designation"],
            prior_nda, fts, len(facts),
        )
        return

    if _update_asset(client, asset_id, payload):
        stats.updated += 1
    else:
        stats.errors += 1


def _load_assets(client: SupabaseClient, *, limit: int, stale_hours: int,
                 asset_id: Optional[str]) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {
        "select": "id,ticker,drug_name,sponsor_name,designations_enriched_at",
        "is_active": "not.is.false",
        "order": "designations_enriched_at.asc.nullsfirst",
        "limit": str(limit),
    }
    if asset_id:
        params["id"] = f"eq.{asset_id}"
    elif stale_hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
        params["or"] = (
            f"(designations_enriched_at.is.null,designations_enriched_at.lt.{cutoff})"
        )
    return client._rest("GET", "fda_assets", params=params) or []


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="enrich_fda_asset_designations")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stale-hours", type=int, default=0,
                   help="Skip assets enriched within the last H hours (0 = all)")
    p.add_argument("--asset-id", default=None, help="Enrich a single asset by id")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    stats = Stats()

    rows = _load_assets(client, limit=args.limit, stale_hours=args.stale_hours,
                        asset_id=args.asset_id)
    stats.assets_seen = len(rows)
    logger.info("Enriching %d active fda_assets", len(rows))

    for row in rows:
        if is_garbage_drug_name(row.get("drug_name")):
            stats.skipped_garbage += 1
            continue
        try:
            enrich_asset(client, row, stats, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 — never let one asset abort the pass
            stats.errors += 1
            logger.warning("enrich failed for %s: %s", row.get("id"), exc)

    logger.info(
        "Designation enrich summary: assets=%d skipped_garbage=%d priority_review=%d "
        "breakthrough=%d first_time_sponsor=%d sponsor_unknown=%d updated=%d errors=%d",
        stats.assets_seen, stats.skipped_garbage, stats.priority_review_true,
        stats.breakthrough_true, stats.first_time_sponsor_true, stats.sponsor_unknown,
        stats.updated, stats.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
