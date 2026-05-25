"""Phase 3a — one-shot earnings_calendar backfill.

Iterates the tradeable-ticker universe (eval_harness.tradeable_filter_pass=true
joined to fda_assets.ticker) and fetches 5 years of earnings dates per ticker.
Designed to be re-run safely; UNIQUE (ticker, earnings_date, source) on the
target table makes upserts idempotent.

Cadence: run once after applying 20260605000010_earnings_calendar_table.sql
to seed the historical record for Q1 audits against the existing 35 frozen
fda_regulatory_events rows.

Run:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.scripts.phase3a_backfill_earnings_calendar \\
        --years-back 5 --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.fetchers.universe.earnings_calendar import (  # noqa: E402
    fetch,
    load_tradeable_tickers,
)
from modal_workers.shared.supabase_client import SupabaseClient  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--years-back", type=int, default=5,
                   help="Backfill window in years (default 5).")
    p.add_argument("--forward-days", type=int, default=90,
                   help="Forward window to also catch next-quarter estimates.")
    p.add_argument("--apply", action="store_true",
                   help="Persist to Supabase. Default is dry-run.")
    p.add_argument("--tickers", default=None,
                   help="Override ticker list (comma-separated). Defaults to "
                        "load_tradeable_tickers().")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = SupabaseClient()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_tradeable_tickers(client)

    if not tickers:
        print("[phase3a backfill] no tradeable tickers — nothing to backfill")
        return 0

    print(f"[phase3a backfill] starting earnings_calendar backfill: "
          f"{len(tickers)} tickers, {args.years_back}y back, "
          f"+{args.forward_days}d forward, apply={args.apply}")
    result = fetch(
        client,
        tickers=tickers,
        lookback_days=365 * args.years_back,
        forward_days=args.forward_days,
        dry_run=not args.apply,
    )
    print(f"[phase3a backfill] done: {result}")
    if result["errors"]:
        print(f"[phase3a backfill] {len(result['errors'])} errors "
              f"(showing first 5): {result['errors'][:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
