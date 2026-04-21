"""One-time backfill for the emissions-ledger foundation (Phase 1b).

Seeds `catalyst_universe` rows for the four known v1 archived post-edge cases
(TVTX, AVNS, GSAT, SEM) so the future coverage auditor has anchors to test
recall against. The v1 archive JSON at:

    unified_system/unified_system/candidates/_curated_rationales.json

contains the `_archived` block these entries were moved into on 2026-04-17
under D-013 (pre-edge-only mandate). Each archived entry carries the catalyst
type, approximate date, and realized price move — enough to populate
catalyst_universe with reasonable provenance.

What this script does NOT do:
  - Label outcomes for v2 candidate rows. None of TVTX/AVNS/GSAT/SEM exist in
    v2 `candidates` (historical import was minimal, per spec.md §9). When a
    future scanner emits a matching signal, the coverage auditor will join
    catalyst_universe ↔ emissions_ledger on ticker+date window.

Idempotent: upserts on (source_feed, catalyst_type, ticker, catalyst_date).
Re-running updates raw_payload but leaves catalyst_id stable.

Dry-run (default):
    python3 migrations/ledger_backfill.py

Live (writes to Supabase):
    SUPABASE_URL=https://xvwvwbnxdsjpnealarkh.supabase.co \\
    SUPABASE_SERVICE_ROLE_KEY=sbp_... \\
    python3 migrations/ledger_backfill.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
V1_ARCHIVE = REPO_ROOT / "unified_system" / "unified_system" / "candidates" / "_curated_rationales.json"

sys.path.insert(0, str(REPO_ROOT))
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402


# --------------------------------------------------------------------
# Per-ticker backfill spec
# --------------------------------------------------------------------
#
# Derived from _curated_rationales.json._archived (2026-04-17 archive batch).
# Materiality threshold per spec: 5% generic, 15% for binary_catalyst.
#
# Label reasoning (reviewable; not applied by this script — no v2 candidate
# rows to attach to):
#   - TVTX  → "pre_edge_hit"    — we had it Active heading into the PDUFA
#                                  (archive_reason starts with DELIVERED), FDA
#                                  approved +34%. Label-worthy when / if the
#                                  v2 pipeline re-emits it retroactively.
#   - AVNS  → "post_edge_miss"  — archive explicitly says MISSED; drove the
#                                  takeover_candidate scanner addition (D-014).
#   - GSAT  → "post_edge_miss"  — deal announced fully priced; no pre-edge
#                                  signal from our pipeline.
#   - SEM   → "unclear"         — <5% spread from day one, sub-threshold for
#                                  merger_arb edge; neutral rather than miss.

BACKFILL_ROWS: List[Dict[str, Any]] = [
    {
        "ticker": "TVTX",
        "profile": "binary_catalyst",
        "catalyst_type": "fda_approval",
        "catalyst_date": "2026-04-13",
        "material_outcome": "yes",
        "realized_price_move": 34.0,   # $30.70 → $41.10
        "price_move_window": "t+1",
        "source_feed": "v1_curated_rationales_archive_backfill",
        "source_url": None,
        "raw_payload": {
            "archived_date": "2026-04-17",
            "archive_reason": (
                "DELIVERED — FDA granted full approval of FILSPARI for FSGS on "
                "April 13, 2026. Stock moved $30.70 → $41.10 (+34%)."
            ),
            "outcome_per_v1_archive": "WIN (if held pre-PDUFA)",
            "former_one_liner": (
                "FDA decision on FILSPARI for rare kidney disease (FSGS); "
                "PDUFA April 13 already passed."
            ),
            "suggested_outcome_label": "pre_edge_hit",
            "note": (
                "No v2 candidate row exists; label_outcome() is a no-op for this "
                "row. Included as a universe anchor for future coverage audits."
            ),
        },
    },
    {
        "ticker": "AVNS",
        "profile": "takeover_candidate",
        "catalyst_type": "take_private_announce",
        "catalyst_date": "2026-04-14",
        "material_outcome": "yes",
        "realized_price_move": 67.0,   # ~$14.50 → ~$24.50 (72% premium; +67% post-announce)
        "price_move_window": "t+1",
        "source_feed": "v1_curated_rationales_archive_backfill",
        "source_url": None,
        "raw_payload": {
            "archived_date": "2026-04-17",
            "archive_reason": (
                "DELIVERED — American Industrial Partners announced $25/share "
                "cash take-private on April 14, 2026 ($1.27B EV, 72% premium). "
                "Spike from ~$14.50 to ~$24.50."
            ),
            "outcome_per_v1_archive": (
                "WIN (if identified pre-announcement). MISSED — system identified "
                "AVNS only as post-announcement merger-arb, too late to capture "
                "premium."
            ),
            "former_one_liner": (
                "Avanos merger arbitrage: going private at $25 cash vs. $24.66 — "
                "a 1.4% spread closable by late 2026."
            ),
            "suggested_outcome_label": "post_edge_miss",
            "lesson_per_v1_archive": (
                "Takeover-candidate scanner should have flagged AVNS 60-90 days "
                "pre-deal: streamlined medtech portfolio, ~$850M mcap, stagnant "
                "stock, divested non-core, clean FCF — classic PE take-private "
                "setup."
            ),
        },
    },
    {
        "ticker": "GSAT",
        "profile": "merger_arb",
        "catalyst_type": "mna_announce",
        "catalyst_date": "2026-04-14",
        "material_outcome": "yes",
        "realized_price_move": None,   # fully-priced close; no t+1 delta meaningfully measurable
        "price_move_window": None,
        "source_feed": "v1_curated_rationales_archive_backfill",
        "source_url": None,
        "raw_payload": {
            "archived_date": "2026-04-17",
            "archive_reason": (
                "DELIVERED — Amazon announced all-cash acquisition at $90/share "
                "on April 14, 2026 ($11.57B). Fully priced."
            ),
            "outcome_per_v1_archive": "WIN (if held pre-announcement)",
            "former_one_liner": (
                "Amazon acquiring Globalstar — mixed cash + stock (0.3x AMZN) "
                "deal with 2-3% spread + embedded AMZN call option."
            ),
            "suggested_outcome_label": "post_edge_miss",
        },
    },
    {
        "ticker": "SEM",
        "profile": "merger_arb",
        "catalyst_type": "take_private_announce",
        "catalyst_date": "2026-03-02",
        "material_outcome": "no",      # 4% spread is sub-threshold per D-013
        "realized_price_move": 4.0,
        "price_move_window": "t+30",
        "source_feed": "v1_curated_rationales_archive_backfill",
        "source_url": None,
        "raw_payload": {
            "archived_date": "2026-04-17",
            "archive_reason": (
                "POST-EDGE — $16.50/share WCAS + Ortenzio take-private announced "
                "March 2, 2026. Stock already at ~$15.90 (4% spread). Merger-arb "
                "spread does not represent a meaningful unpriced edge."
            ),
            "outcome_per_v1_archive": "NEUTRAL — merger-arb only, thin spread",
            "former_one_liner": (
                "Select Medical take-private by WCAS + Ortenzio family at ~$16.50 "
                "cash — low-risk LBO with ~3% spread."
            ),
            "suggested_outcome_label": "unclear",
        },
    },
]


# --------------------------------------------------------------------
# Entity resolution (best-effort — catalyst_universe.entity_id is optional)
# --------------------------------------------------------------------

def lookup_entity_id(client: SupabaseClient, ticker: str) -> Optional[str]:
    """Look up entities.id by primary_ticker. Returns None if not found."""
    try:
        rows = client._rest(
            "GET", "entities",
            params={
                "primary_ticker": f"eq.{ticker}",
                "select": "id,primary_mic,name",
                "limit": "1",
            },
        )
    except SupabaseError:
        return None
    if not rows:
        return None
    return rows[0]["id"]


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Supabase. Default is dry-run (print only).",
    )
    args = parser.parse_args()

    # Sanity-check against the v1 archive JSON — if it drifts we want to know
    # before writing anchors derived from a stale snapshot.
    if not V1_ARCHIVE.exists():
        print(f"[error] v1 archive JSON not found at {V1_ARCHIVE}", file=sys.stderr)
        return 1
    with V1_ARCHIVE.open() as fh:
        data = json.load(fh)
    archived = data.get("_archived") or {}
    expected = {row["ticker"] for row in BACKFILL_ROWS}
    actual = set(archived.keys())
    if expected != actual:
        print(
            f"[warn] v1 archive tickers {sorted(actual)} != backfill spec "
            f"{sorted(expected)} — archive may have grown since this script was "
            f"written; review before applying.",
            file=sys.stderr,
        )

    print("=" * 64)
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 64)

    for row in BACKFILL_ROWS:
        print(
            f"  {row['ticker']:6s}  profile={row['profile']:<22s}  "
            f"type={row['catalyst_type']:<22s}  date={row['catalyst_date']}  "
            f"material={row['material_outcome']}  "
            f"move={row['realized_price_move']}  "
            f"label={row['raw_payload'].get('suggested_outcome_label')}"
        )

    if not args.apply:
        print("\n(dry-run — re-run with --apply to write to Supabase)")
        return 0

    client = SupabaseClient()
    written = 0
    for row in BACKFILL_ROWS:
        entity_id = lookup_entity_id(client, row["ticker"])
        try:
            catalyst_id = upsert_catalyst_universe_row(
                client,
                profile=row["profile"],
                catalyst_type=row["catalyst_type"],
                catalyst_date=row["catalyst_date"],
                source_feed=row["source_feed"],
                ticker=row["ticker"],
                entity_id=entity_id,
                material_outcome=row["material_outcome"],
                realized_price_move=row["realized_price_move"],
                price_move_window=row["price_move_window"],
                source_url=row["source_url"],
                raw_payload=row["raw_payload"],
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {row['ticker']}: {e}", file=sys.stderr)
            continue
        entity_note = f" entity_id={entity_id}" if entity_id else " (no entity match)"
        print(f"  [ok]    {row['ticker']:6s}  catalyst_id={catalyst_id}{entity_note}")
        written += 1

    print(f"\nWrote/updated {written} of {len(BACKFILL_ROWS)} catalyst_universe rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
