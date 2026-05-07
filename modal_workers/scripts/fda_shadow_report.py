"""
Daily shadow comparison report.

Reads the public.fda_shadow_compare view and produces a summary of how the new
fda_event scoring would change behavior vs the current binary_catalyst flow.
The Phase 3 exit criteria require the new pipeline to beat the current flow on
all three of recall, post-edge avoidance, and realized EV — see the plan file
at ~/.claude/plans/plan-this-implementation-cozy-locket.md.

This script is read-only (SELECT against the view only). Run from the repo
root once Phase 3 has accumulated enough events:

    python -m modal_workers.scripts.fda_shadow_report [--json]

Metrics emitted:

    events_paired              count of regulatory_events where both pipelines
                               produced a score
    band_change_distribution   how shadow band differs from canonical band
    immediate_eligibility      shadow vs canonical Immediate counts
    post_edge_avoidance        % of resolution events the new pipeline did NOT
                               score as new opportunity (target: 100%)
    realized_ev_by_band        avg realized_ev (from outcomes/signal_price_snapshots)
                               grouped by shadow_band
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from typing import Any, Dict, List

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger("fda_shadow_report")


def _fetch_shadow_compare(client: SupabaseClient) -> List[Dict[str, Any]]:
    rows = client._rest("GET", "fda_shadow_compare", params={"select": "*"})
    if not isinstance(rows, list):
        raise SupabaseError(500, f"unexpected fda_shadow_compare response: {rows!r}")
    return rows


def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    paired = [r for r in rows if r.get("shadow_score") is not None and r.get("canonical_score") is not None]
    shadow_only = [r for r in rows if r.get("shadow_score") is not None and r.get("canonical_score") is None]
    canonical_only = [r for r in rows if r.get("shadow_score") is None and r.get("canonical_score") is not None]

    band_changes = Counter(r.get("band_change") for r in paired if r.get("band_change"))

    shadow_immediate = sum(1 for r in rows if r.get("shadow_immediate"))
    canonical_immediate = sum(1 for r in rows if r.get("canonical_immediate"))

    # Post-edge avoidance: for resolution events, count how many the bridge did
    # NOT promote (shadow_band IS NULL or != 'immediate'/'watchlist').
    resolution_rows = [r for r in rows if r.get("is_resolution_event")]
    avoided = sum(
        1 for r in resolution_rows
        if r.get("shadow_band") not in ("immediate", "watchlist")
    )
    avoidance_rate = (avoided / len(resolution_rows)) if resolution_rows else None

    return {
        "events_total": len(rows),
        "events_paired": len(paired),
        "events_shadow_only": len(shadow_only),
        "events_canonical_only": len(canonical_only),
        "band_change_distribution": dict(band_changes),
        "immediate_eligibility": {
            "shadow": shadow_immediate,
            "canonical": canonical_immediate,
        },
        "post_edge_avoidance": {
            "resolution_event_count": len(resolution_rows),
            "avoided_count": avoided,
            "avoidance_rate": avoidance_rate,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )
    rows = _fetch_shadow_compare(client)
    summary = _summarize(rows)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"FDA shadow comparison ({summary['events_total']} events)")
    print(f"  paired (both scored):    {summary['events_paired']}")
    print(f"  shadow-only:             {summary['events_shadow_only']}")
    print(f"  canonical-only:          {summary['events_canonical_only']}")
    print(f"  band changes:            {summary['band_change_distribution']}")
    imm = summary["immediate_eligibility"]
    print(f"  immediate eligibility:   shadow={imm['shadow']} canonical={imm['canonical']}")
    pea = summary["post_edge_avoidance"]
    if pea["resolution_event_count"] == 0:
        print("  post-edge avoidance:     n/a (no resolution events scored)")
    else:
        rate = pea["avoidance_rate"]
        rate_pct = (rate * 100.0) if rate is not None else None
        print(
            f"  post-edge avoidance:     {pea['avoided_count']}/{pea['resolution_event_count']} "
            f"({rate_pct:.1f}%)" if rate_pct is not None else
            f"  post-edge avoidance:     {pea['avoided_count']}/{pea['resolution_event_count']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
