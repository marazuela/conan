"""
One-shot backfill: read pdufa_watchlist.json from Supabase Storage and populate
fda_assets / fda_regulatory_events / fda_event_evidence.

The watchlist JSON is the v1 source of truth (Supabase Storage at
scanner-caches/fda/pdufa_watchlist.json). After this backfill runs, Postgres is
authoritative; the JSON stays as a rollback-only export.

Idempotent: assets upsert on (ticker, drug_name, application_number); events
upsert on (asset_id, event_type, event_date, source_content_hash); evidence
upserts on (event_id, source, hash).

Usage (one-shot, run from repo root after migrations are applied):

    python -m modal_workers.scripts.fda_backfill_watchlist [--dry-run] [--from-file PATH]

Without --from-file, reads from Supabase Storage scanner-caches/fda/pdufa_watchlist.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List

from modal_workers.scanners.fda_event_state import (
    AssetRow,
    EventRow,
    EvidenceRow,
    TransformResult,
    transform_watchlist_payload,
)
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger("fda_backfill_watchlist")

WATCHLIST_CACHE_PREFIX = "scanner-caches"
WATCHLIST_CACHE_KEY = "fda/pdufa_watchlist.json"


def _load_payload(client: SupabaseClient, from_file: str | None) -> List[Dict[str, Any]]:
    if from_file:
        with open(from_file, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    raw = client.read_cache(WATCHLIST_CACHE_PREFIX, WATCHLIST_CACHE_KEY)
    if raw is None:
        raise RuntimeError(
            f"watchlist cache empty at {WATCHLIST_CACHE_PREFIX}/{WATCHLIST_CACHE_KEY}"
        )
    return json.loads(raw.decode("utf-8"))


def _upsert_assets(
    client: SupabaseClient, assets: List[AssetRow], dry_run: bool
) -> Dict[str, str]:
    """Upsert assets. Returns mapping (ticker|drug|app_num) -> asset uuid."""
    if not assets:
        return {}
    body = []
    for a in assets:
        body.append(
            {
                "ticker": a["ticker"],
                "mic": a.get("mic"),
                "drug_name": a["drug_name"],
                "application_number": a.get("application_number", ""),
                "application_type": a.get("application_type"),
                "indication": a.get("indication"),
                "sponsor_name": a.get("sponsor_name"),
                "extensions": a.get("extensions") or {},
            }
        )
    if dry_run:
        logger.info("dry-run: would upsert %d fda_assets rows", len(body))
        return {f"{r['ticker']}|{r['drug_name']}|{r['application_number']}": "DRY" for r in body}

    rows = client._rest_with_retry(
        "POST",
        "fda_assets?on_conflict=ticker,drug_name,application_number",
        json_body=body,
        prefer="resolution=merge-duplicates,return=representation",
    )
    if not isinstance(rows, list):
        raise SupabaseError(500, f"unexpected fda_assets upsert response: {rows!r}")
    return {f"{r['ticker']}|{r['drug_name']}|{r['application_number']}": r["id"] for r in rows}


def _upsert_events(
    client: SupabaseClient,
    events: List[EventRow],
    asset_ids: Dict[str, str],
    dry_run: bool,
) -> Dict[str, str]:
    """Upsert events. Returns mapping event_key -> event uuid."""
    if not events:
        return {}
    body = []
    keys = []
    for e in events:
        asset_id = asset_ids.get(e["asset_key"])
        if asset_id is None or asset_id == "DRY":
            if dry_run:
                continue
            raise RuntimeError(f"asset_id missing for event asset_key={e['asset_key']!r}")
        body.append(
            {
                "asset_id": asset_id,
                "event_type": e["event_type"],
                "event_date": e.get("event_date"),
                "event_status": e["event_status"],
                "source_content_hash": e["source_content_hash"],
                "notes": e.get("notes"),
                "extensions": e.get("extensions") or {},
            }
        )
        keys.append(f"{e['asset_key']}|{e['event_type']}|{e.get('event_date') or ''}")
    if dry_run:
        logger.info("dry-run: would upsert %d fda_regulatory_events rows", len(events))
        return {k: "DRY" for k in keys}
    rows = client._rest_with_retry(
        "POST",
        "fda_regulatory_events?on_conflict=asset_id,event_type,event_date,source_content_hash",
        json_body=body,
        prefer="resolution=merge-duplicates,return=representation",
    )
    if not isinstance(rows, list):
        raise SupabaseError(500, f"unexpected fda_regulatory_events upsert response: {rows!r}")
    out: Dict[str, str] = {}
    for k, r in zip(keys, rows):
        out[k] = r["id"]
    return out


def _upsert_evidence(
    client: SupabaseClient,
    evidence: List[EvidenceRow],
    event_ids: Dict[str, str],
    dry_run: bool,
) -> int:
    if not evidence:
        return 0
    body = []
    for ev in evidence:
        event_id = event_ids.get(ev["event_key"])
        if event_id is None or event_id == "DRY":
            if dry_run:
                continue
            raise RuntimeError(f"event_id missing for evidence event_key={ev['event_key']!r}")
        body.append(
            {
                "event_id": event_id,
                "source": ev["source"],
                "evidence_type": ev["evidence_type"],
                "payload": ev["payload"],
                "hash": ev["hash"],
            }
        )
    if dry_run:
        logger.info("dry-run: would upsert %d fda_event_evidence rows", len(evidence))
        return len(evidence)
    client._rest_with_retry(
        "POST",
        "fda_event_evidence?on_conflict=event_id,source,hash",
        json_body=body,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    return len(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Skip DB writes, just log counts.")
    parser.add_argument("--from-file", help="Read watchlist JSON from a local file instead of Storage.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )

    payload = _load_payload(client, args.from_file)
    logger.info("loaded %d watchlist rows", len(payload))

    result: TransformResult = transform_watchlist_payload(payload)
    logger.info(
        "transform result: assets=%d events=%d evidence=%d",
        len(result["assets"]),
        len(result["events"]),
        len(result["evidence"]),
    )

    asset_ids = _upsert_assets(client, result["assets"], args.dry_run)
    event_ids = _upsert_events(client, result["events"], asset_ids, args.dry_run)
    n_evidence = _upsert_evidence(client, result["evidence"], event_ids, args.dry_run)
    logger.info(
        "wrote: assets=%d events=%d evidence=%d (dry_run=%s)",
        len(asset_ids),
        len(event_ids),
        n_evidence,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
