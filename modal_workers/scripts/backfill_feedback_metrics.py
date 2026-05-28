"""Backfill feedback_category_metrics for v4 Phase 8a wiring.

The daily_feedback_loop chain's new 4th step writes ONE snapshot row per
(profile, signal_category, horizon) per call — i.e. today's snapshot only.
The first weekly Cowork retro (Sunday 20:00 UTC) wants ~30 days of history
to plot trends and detect category drift. This one-shot backfill walks
backwards and persists a snapshot per past day so the retro has a real
time series to reason over.

Idempotent: feedback_category_metrics has a UNIQUE constraint on
(snapshot_date, profile, signal_category, horizon_days) and persist_*
uses Prefer: resolution=merge-duplicates, so re-running just overwrites.

Usage:
  python -m modal_workers.scripts.backfill_feedback_metrics --days 30
  python -m modal_workers.scripts.backfill_feedback_metrics --days 90 \\
      --cohort-days 90 --dry-run

Plan: ~/.claude/plans/phases-6-and-7-staged-hedgehog.md (Phase 8a).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from modal_workers.feedback.category_accuracy import (
    DEFAULT_COHORT_DAYS,
    DEFAULT_HORIZONS,
    aggregate_by_category,
    load_post_mortem_rows,
    persist_category_metrics,
)
from modal_workers.shared.supabase_client import SupabaseClient


logger = logging.getLogger(__name__)


def backfill(
    *,
    days: int,
    cohort_days: int = DEFAULT_COHORT_DAYS,
    end_date: date | None = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Iterate snapshot_date backwards from end_date for `days` days.

    For each past snapshot_date, compute the cohort window ending on that
    date and persist the per-category metrics.
    """
    sb = SupabaseClient()
    end_date = end_date or datetime.now(timezone.utc).date()
    out: List[Dict[str, Any]] = []
    for offset in range(days):
        snap_day = end_date - timedelta(days=offset)
        cohort_start = snap_day - timedelta(days=cohort_days)
        rows = load_post_mortem_rows(
            sb,
            cohort_window_start=cohort_start,
            cohort_window_end=snap_day,
        )
        metrics = aggregate_by_category(rows, horizons=DEFAULT_HORIZONS)
        if dry_run:
            persisted = 0
        else:
            persisted = persist_category_metrics(
                sb,
                snapshot_date=snap_day,
                cohort_window_start=cohort_start,
                cohort_window_end=snap_day,
                metrics=metrics,
            )
        rec = {
            "snapshot_date": snap_day.isoformat(),
            "cohort_window_start": cohort_start.isoformat(),
            "input_rows": len(rows),
            "metric_cells": len(metrics),
            "rows_persisted": persisted,
        }
        logger.info(
            "backfill snapshot_date=%s rows=%d cells=%d persisted=%d",
            snap_day, len(rows), len(metrics), persisted,
        )
        out.append(rec)
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30,
                   help="How many past snapshot dates to backfill (default 30).")
    p.add_argument("--cohort-days", type=int, default=DEFAULT_COHORT_DAYS,
                   help=f"Trailing cohort window per snapshot (default {DEFAULT_COHORT_DAYS}).")
    p.add_argument("--end-date", type=str, default=None,
                   help="ISO date of the most recent snapshot to backfill "
                        "(default today UTC).")
    p.add_argument("--dry-run", action="store_true",
                   help="Aggregate but skip the persist call.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    results = backfill(
        days=args.days,
        cohort_days=args.cohort_days,
        end_date=end_date,
        dry_run=args.dry_run,
    )
    total_input = sum(r["input_rows"] for r in results)
    total_cells = sum(r["metric_cells"] for r in results)
    total_persisted = sum(r["rows_persisted"] for r in results)
    logger.info(
        "backfill done: snapshots=%d input_rows_total=%d metric_cells_total=%d persisted_total=%d (dry_run=%s)",
        len(results), total_input, total_cells, total_persisted, args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
