"""seed_eval_harness_from_export — D-109 Phase 1C first-half ETL.

Walks the export's `binary_catalyst.json` (1502 events with ticker + filed_at
but no realized outcome), runs the D-116 forward-return labeler over each
event, and writes a STAGING ledger to disk. Phase 4B picks up the staging
file once `documents` is backfilled and `asset_id` resolution is wired —
that pass is what does the actual `eval_harness` INSERT.

Why staging instead of direct insert: `eval_harness.document_set uuid[]` and
`eval_harness.asset_id` are both NOT NULL. Until Phase 4B populates the
documents table for the historical events and resolves ticker → fda_assets,
we cannot legally insert. The staging file is the contract between Phase 1C
(this script) and Phase 4B (`backfill_documents_for_eval_harness.py`).

Output schema (one record per event):

    {
      "event_id":          str | None,        # from binary_catalyst.json
      "ticker":            str,
      "filed_at":          str,                # ISO date
      "profile":           "binary_catalyst",
      "label":             ForwardReturnLabel.as_dict(),
      "skip_category":     str | None,         # set when hit is None
      "asset_id":          null,               # filled by Phase 4B
      "document_set":      null,               # filled by Phase 4B
      "tradeable_filter_pass": null,           # D-113 / Phase 4B
    }

Skip categories (mirrors export methodology):
  - no_price_data        — yfinance returned nothing for ticker+window
  - unparseable_date     — filed_at not parseable
  - anchor_unresolved    — ticker history predates the event
  - delisted             — emit a -100% verdict (still labeled, NOT skipped)

Usage:

    python -m modal_workers.scripts.seed_eval_harness_from_export \\
        --events  ~/Downloads/_EXPORT_skills_scoring_methodology/data/v2_data/historical_events/binary_catalyst.json \\
        --output  data/eval_harness_staging/binary_catalyst.json \\
        [--limit 50] [--dry-run]

The script is idempotent — re-running overwrites the output file. Failures
are recorded per-event in the staging file (skip_category populated, label
left null) so Phase 4B can decide whether to retry vs drop.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _load_events(events_path: Path) -> List[Dict[str, Any]]:
    """Read the export's events ledger. Tolerates {events:[...]} or [...]."""
    if not events_path.exists():
        raise FileNotFoundError(f"events file not found: {events_path}")
    raw = json.loads(events_path.read_text())
    if isinstance(raw, list):
        return raw
    return raw.get("events", [])


def _categorize_skip(label: Dict[str, Any]) -> Optional[str]:
    """Pick a skip category for events the labeler couldn't resolve to a
    HIT/MISS verdict. Mirrors `label_forward_returns._serializable_label`."""
    if label.get("hit") is True or label.get("hit") is False:
        return None
    miss = (label.get("miss_reason") or "").lower()
    if "no_price_data" in miss or "no price" in miss:
        return "no_price_data"
    if "unparseable" in miss or "filed_at" in miss:
        return "unparseable_date"
    if "anchor" in miss:
        return "anchor_unresolved"
    if "no_spy" in miss:
        return "no_spy"
    if "ma_outcome_pending" in miss:
        return "ma_outcome_pending"
    if "delisted" in miss:
        return "delisted_no_window"
    if "private_discard" in miss or "unresolvable" in miss:
        return "private_or_unresolvable"
    if "missing_ticker_or_filed_at" in miss:
        return "missing_required_field"
    if "unresolved_ticker_sentinel" in miss:
        return "ticker_sentinel"
    return "other"


def _build_staging_record(
    event: Dict[str, Any], label: Dict[str, Any],
) -> Dict[str, Any]:
    """Compose one staging record."""
    skip = _categorize_skip(label)
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "ticker": event.get("ticker") or label.get("ticker"),
        "filed_at": event.get("filed_at") or label.get("filed_at"),
        "profile": "binary_catalyst",
        "label": label,
        "skip_category": skip,
        # Phase 4B fills these:
        "asset_id": None,
        "document_set": None,
        "tradeable_filter_pass": None,
    }


def seed(
    *,
    events_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Main loop: load events → label_ledger → staging records → write."""
    from modal_workers.scripts.label_forward_returns import label_ledger

    events = _load_events(events_path)
    if limit is not None:
        events = events[:limit]

    logger.info("seed: %d events from %s", len(events), events_path)

    # `label_ledger` returns a list of ForwardReturnLabel.as_dict() shapes,
    # paired index-aligned with `events` input order.
    labels = label_ledger(
        events=events,
        profile="binary_catalyst",
    )

    by_skip: Counter = Counter()
    by_hit: Counter = Counter()
    staging: List[Dict[str, Any]] = []
    for ev, lb in zip(events, labels):
        rec = _build_staging_record(ev, lb)
        if rec["skip_category"]:
            by_skip[rec["skip_category"]] += 1
        if lb.get("hit") is True:
            by_hit["HIT"] += 1
        elif lb.get("hit") is False:
            by_hit["MISS"] += 1
        else:
            by_hit["UNRESOLVED"] += 1
        staging.append(rec)

    summary = {
        "events_seen": len(events),
        "labels_resolved": by_hit["HIT"] + by_hit["MISS"],
        "by_hit": dict(by_hit),
        "by_skip": dict(by_skip),
        "staging_path": str(output_path),
    }

    if dry_run:
        logger.info("[dry-run] would write %d staging records", len(staging))
        return summary

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({
        "_meta": {
            "source_events": str(events_path),
            "profile": "binary_catalyst",
            "labeler_version": "label_forward_returns.v0.1",
            "summary": summary,
        },
        "staging": staging,
    }, indent=2))
    logger.info("wrote %d staging records to %s", len(staging), output_path)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", type=Path, required=True,
                   help="path to export's binary_catalyst.json")
    p.add_argument("--output", type=Path, required=True,
                   help="staging ledger path (will be created)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap event count (debugging)")
    p.add_argument("--dry-run", action="store_true",
                   help="print summary, do not write output")
    args = p.parse_args(argv)

    summary = seed(
        events_path=args.events, output_path=args.output,
        limit=args.limit, dry_run=args.dry_run,
    )
    logger.info("summary: %s", summary)
    # exit non-zero only when zero events resolved (all skips) — that's a
    # signal something is structurally wrong with the labeler input.
    return 0 if summary["labels_resolved"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
