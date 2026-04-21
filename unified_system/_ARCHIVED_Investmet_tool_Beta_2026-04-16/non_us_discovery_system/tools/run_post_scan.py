"""
Post-scan routine that runs AFTER all scheduled scanners have produced their
processed signal JSONs for the day. Responsibilities:

1. Consolidate the day's processed signals across scanners into a single
   day-level summary.
2. Cross-scanner convergence check — re-run convergence_engine against the day's
   union so that signals that converged across different scanners (e.g., LSE +
   HKEx dual-listing) pick up their bonus even if each scanner ran in isolation.
3. Emit `signals/daily_summary_<YYYY-MM-DD>.json` for the performance-report
   skill to consume.

This is invoked by the operational skill at the end of its run, once per day
(idempotent within the day — calling it multiple times overwrites the summary).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools import convergence_engine

ROOT = Path(__file__).parent.parent
SIGNALS_DIR = ROOT / "signals"


def consolidate(scan_date: str | None = None) -> dict:
    if scan_date is None:
        scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # gather all processed files for the date
    day_files = sorted(SIGNALS_DIR.glob(f"*_{scan_date}_processed.json"))
    all_signals: list[dict] = []
    per_scanner: dict[str, int] = {}
    for p in day_files:
        try:
            sigs = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # scanner name is filename prefix before "_YYYY-MM-DD_processed"
        name = p.name.split(f"_{scan_date}_processed")[0]
        per_scanner[name] = len(sigs)
        all_signals.extend(sigs)

    # re-run convergence across the union (historical already baked in during per-scanner runs,
    # but cross-scanner convergence that lands on the same day needs re-annotation)
    reprocessed = convergence_engine.annotate_convergence(
        [s for s in all_signals if not s.get("dedup_dropped")],
        historical_signals=[],
    )

    # rebuild counts
    route_counts = {"immediate": 0, "watchlist": 0, "archive": 0, "discard": 0, "manual_review": 0}
    convergence_events = []
    for sig in reprocessed:
        if sig.get("convergence_bonus", 0) > 0:
            convergence_events.append({
                "issuer_figi": sig.get("issuer_figi"),
                "ticker_plus_mic": sig.get("ticker_plus_mic"),
                "strategy_count": sig.get("convergence_strategy_count"),
                "bonus": sig.get("convergence_bonus"),
                "score_total": sig.get("score_total"),
            })
        routing = sig.get("_routing", "manual_review")
        route_counts[routing] = route_counts.get(routing, 0) + 1

    summary = {
        "scan_date": scan_date,
        "per_scanner_counts": per_scanner,
        "total_signals": len(all_signals),
        "deduped_survivors": len(reprocessed),
        "route_counts": route_counts,
        "convergence_events": convergence_events,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    out_path = SIGNALS_DIR / f"daily_summary_{scan_date}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    return summary


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-date", default=None, help="YYYY-MM-DD; defaults to today UTC")
    args = parser.parse_args()
    summary = consolidate(args.scan_date)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
