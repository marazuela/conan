"""One-shot backfill for fda_assets.mechanism via openFDA /drug/label.

Sister script to backfill_indications.py — same flow, different target
column. As of 2026-05-26 all 141 fda_assets rows had mechanism IS NULL,
which silently zeroed the bc_class_precedent_base_rates table the
bc_class_precedent_refresher (PR #148) was meant to populate
(refresher drops every row where moa is empty).

For each asset with mechanism NULL we:
  1. Query openFDA /drug/label by application_number (NDA/BLA prefix
     auto-tried for bare digits). Fall back to brand_name if the
     application_number is synthetic (8K_DERIVED_*) or unresolved.
  2. Extract a single normalized MoA string from the label
     (extract_mechanism_from_label).
  3. PATCH fda_assets.mechanism, scoped to id=eq.<row> AND mechanism=is.null
     so a concurrent operator edit can never be clobbered.

Junk asset rows (drug_name in KNOWN_GARBAGE_DRUG_NAMES — see
backfill_v3_assessments.py) are skipped entirely. They need GC upstream,
not enrichment.

Run:
  python3 -m modal_workers.scripts.backfill_fda_asset_mechanism [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from modal_workers.ingestion.openfda_ingest import extract_mechanism_from_label
from modal_workers.scripts.backfill_indications import (
    _APPL_NUM_RE,
    fetch_label_for_application,
    fetch_label_for_brand,
)
from modal_workers.scripts.backfill_v3_assessments import (
    KNOWN_GARBAGE_DRUG_NAMES,
    is_garbage_drug_name,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    assets_seen: int = 0
    skipped_garbage: int = 0
    label_fetched: int = 0
    label_missing: int = 0
    mechanism_extracted: int = 0
    mechanism_updated: int = 0
    errors: int = 0


def update_asset_mechanism(
    asset_id: str,
    mechanism: str,
    client: SupabaseClient,
) -> bool:
    """PATCH fda_assets.mechanism, guarded by mechanism=is.null so a
    concurrent operator-curated value is never overwritten. Returns True
    when at least one row matched."""
    try:
        rows = client._rest(
            "PATCH", "fda_assets",
            params={
                "id": f"eq.{asset_id}",
                "mechanism": "is.null",
                "select": "id",
            },
            json_body={"mechanism": mechanism},
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PATCH fda_assets failed for %s: %s", asset_id, exc)
        return False
    return bool(rows)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_fda_asset_mechanism")
    p.add_argument("--limit", type=int, default=500,
                   help="Max assets to backfill in one run")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch labels and print proposed updates without writing")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    stats = Stats()

    rows = client._rest(
        "GET", "fda_assets",
        params={
            "select": "id,ticker,drug_name,generic_name,application_number,mechanism",
            "mechanism": "is.null",
            "limit": str(args.limit),
        },
    ) or []
    stats.assets_seen = len(rows)
    logger.info("Found %d fda_assets rows with mechanism IS NULL "
                "(known garbage drug names: %s)",
                len(rows), sorted(KNOWN_GARBAGE_DRUG_NAMES))

    for row in rows:
        asset_id = row["id"]
        appl = (row.get("application_number") or "").strip()
        drug_name = (row.get("drug_name") or "").strip()
        generic_name = (row.get("generic_name") or "").strip()

        if is_garbage_drug_name(drug_name):
            logger.info("Skipping junk drug_name=%r asset=%s", drug_name, asset_id)
            stats.skipped_garbage += 1
            continue

        label: Optional[Dict[str, Any]] = None
        if appl and _APPL_NUM_RE.match(appl):
            label = fetch_label_for_application(appl)
        if not label and drug_name:
            label = fetch_label_for_brand(drug_name)
        if not label and generic_name and generic_name != drug_name:
            label = fetch_label_for_brand(generic_name)
        if not label:
            stats.label_missing += 1
            continue
        stats.label_fetched += 1

        mechanism = extract_mechanism_from_label(label)
        if not mechanism:
            stats.label_missing += 1
            continue
        stats.mechanism_extracted += 1

        if args.dry_run:
            logger.info(
                "[dry-run] %s/%s appl=%s -> '%s'",
                row.get("ticker"), row.get("drug_name"), appl,
                mechanism[:120],
            )
            continue

        if update_asset_mechanism(asset_id, mechanism, client):
            stats.mechanism_updated += 1
        else:
            stats.errors += 1

    logger.info(
        "Mechanism backfill summary: assets=%d skipped_garbage=%d "
        "label_fetched=%d label_missing=%d extracted=%d updated=%d errors=%d",
        stats.assets_seen, stats.skipped_garbage,
        stats.label_fetched, stats.label_missing,
        stats.mechanism_extracted, stats.mechanism_updated, stats.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
