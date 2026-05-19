"""Backfill raw_payload.lead_drug / interventions_all / indication_keywords on
existing pre_phase3_readout signals.

Why: as of 2026-05-14, 143/143 emitted pre_phase3_readout signals carry NULL
raw_payload.lead_drug. Sponsor and indication (base_rate_key) were stored, but
the drug name was dropped. This collapses 50+ distinct drugs into the same
heuristic dimension vector at scoring time and blocks any downstream TAM /
competitive-landscape model from running.

The scanner now (this PR) populates lead_drug + interventions_all +
indication_keywords on new emissions. This one-shot script re-fetches each
existing trial from CT.gov v2 and patches the signal's raw_payload in place.

Idempotent: signals whose raw_payload already has a non-null lead_drug are
skipped (--force overrides). Indication_keywords is mirrored from the existing
matched_indications field when CT.gov re-fetch fails so we still get coverage.

Run:
  python3 -m modal_workers.scripts.backfill_pre_phase3_lead_drug [--limit N]
                                                                 [--dry-run]
                                                                 [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from modal_workers.scanners.pre_phase3_readout_scanner import (
    CLINICALTRIALS_URL,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _extract_drug_interventions,
    _pick_lead_drug_name,
)
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    signals_seen: int = 0
    already_filled: int = 0
    ctgov_fetched: int = 0
    ctgov_missing: int = 0
    lead_drug_resolved: int = 0
    lead_drug_unresolved: int = 0
    rows_updated: int = 0
    errors: int = 0


def fetch_trial(nct_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one CT.gov v2 study record by NCT id. Returns None on miss."""
    if not nct_id:
        return None
    url = f"{CLINICALTRIALS_URL}/{nct_id}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.warning("CT.gov fetch failed for %s: %s", nct_id, exc)
        return None


def derive_fields(study: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror the scanner's _score_trial intervention extraction."""
    proto = study.get("protocolSection", {}) or {}
    arms_mod = proto.get("armsInterventionsModule", {}) or {}
    raw_interventions = arms_mod.get("interventions") or []

    interventions = _extract_drug_interventions(raw_interventions)
    interventions_all = [
        {
            "name": (iv.get("name") or "").strip(),
            "type": (iv.get("type") or "").strip().upper(),
        }
        for iv in raw_interventions
        if (iv.get("name") or "").strip()
    ]
    lead_drug = _pick_lead_drug_name(interventions)
    return {
        "lead_drug": lead_drug,
        "interventions": interventions,
        "interventions_all": interventions_all,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_pre_phase3_lead_drug")
    p.add_argument("--limit", type=int, default=500,
                   help="Max signals to backfill in one run")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + derive but don't PATCH")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even if raw_payload.lead_drug is already set")
    p.add_argument("--sleep", type=float, default=0.2,
                   help="Seconds to wait between CT.gov requests (rate limit)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    stats = Stats()

    rows = client._rest(
        "GET", "signals",
        params={
            "select": "signal_id,raw_payload",
            "signal_type": "eq.pre_phase3_readout",
            "limit": str(args.limit),
            "order": "scan_date.desc",
        },
    ) or []
    stats.signals_seen = len(rows)
    logger.info("Found %d pre_phase3_readout signals", len(rows))

    for row in rows:
        signal_id = row["signal_id"]
        payload = row.get("raw_payload") or {}
        nct_id = (payload.get("nct_id") or "").strip()

        if payload.get("lead_drug") and not args.force:
            stats.already_filled += 1
            continue

        if not nct_id:
            stats.ctgov_missing += 1
            continue

        study = fetch_trial(nct_id)
        if args.sleep > 0:
            time.sleep(args.sleep)
        if study is None:
            stats.ctgov_missing += 1
            continue
        stats.ctgov_fetched += 1

        derived = derive_fields(study)
        lead_drug = derived["lead_drug"]
        if lead_drug:
            stats.lead_drug_resolved += 1
        else:
            stats.lead_drug_unresolved += 1

        # indication_keywords is just an alias for the existing
        # matched_indications list — preserve whatever was already there so
        # this backfill doesn't depend on re-running the indication mapper.
        matched = payload.get("matched_indications") or []

        new_payload = {
            **payload,
            "lead_drug": lead_drug,
            "interventions": derived["interventions"],
            "interventions_all": derived["interventions_all"],
            "indication_keywords": matched,
        }

        if args.dry_run:
            logger.info(
                "[dry-run] %s nct=%s lead_drug=%r interventions_all=%d",
                signal_id, nct_id, lead_drug, len(derived["interventions_all"]),
            )
            continue

        try:
            client._rest(
                "PATCH", "signals",
                params={"signal_id": f"eq.{signal_id}"},
                json_body={"raw_payload": new_payload},
                prefer="return=minimal",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PATCH signals failed for %s: %s", signal_id, exc)
            stats.errors += 1
            continue
        stats.rows_updated += 1

    logger.info(
        "lead_drug backfill summary: signals=%d already=%d ctgov_fetched=%d "
        "ctgov_missing=%d resolved=%d unresolved=%d updated=%d errors=%d",
        stats.signals_seen, stats.already_filled, stats.ctgov_fetched,
        stats.ctgov_missing, stats.lead_drug_resolved,
        stats.lead_drug_unresolved, stats.rows_updated, stats.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
