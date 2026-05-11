"""seed_eval_harness_db_etl — Phase 4B subset ETL for the staged binary_catalyst.

Loads the staging ledger written by `seed_eval_harness_from_export.py` into
the live `eval_harness` table for the SUBSET of records where the ticker
resolves to an existing `fda_assets` row. Other records stay as a JSON
contract on disk until either (a) the matching `fda_assets` row exists or
(b) a future ETL pass relaxes the schema.

Why a subset and not full 1502: `eval_harness.asset_id NOT NULL` and
`eval_harness.document_set uuid[] NOT NULL` are both hard constraints. We
write `document_set='{}'::uuid[]` (empty array satisfies NOT NULL) so
`backfill_document_set.py` can patch the array later via the existing
adapter sweep. We require an existing `fda_assets.ticker` match because
without it we have no asset_id to write.

Multi-asset tickers (PFE=7 fda_assets, AZN=6, etc.) are ambiguous: a
binary-catalyst event filed against ticker PFE could reference any of
seven drug assets. Three modes are offered:

  - `newest` (default): pick the freshest `fda_assets.id` per ticker (max
    `created_at`). Heuristic; mis-attributes when the historical event was
    really about a different asset. Acceptable for a calibration-set seed
    expansion since the realized outcome (the price-window verdict) is
    ticker-level, not asset-level.
  - `skip`: skip multi-asset tickers entirely. Most conservative; loses
    ~80% of the matched subset (most matches are big-pharma multi-asset).
  - `active`: prefer `is_active=true` rows; fall back to `newest`.

Idempotency: every INSERT checks `(asset_id, reference_assessment_date)`
first and skips when a row already exists, so re-running is safe.

The 81 curated holdout rows (`is_holdout=true`) are NOT touched by this
script — they keep their identity as the Phase 0 gold-standard set. Rows
this script writes carry `is_holdout=false` and `notes` prefixed with
`phase4b_seed:` so dashboards can filter them out of the holdout count.

Usage:

    python -m modal_workers.scripts.seed_eval_harness_db_etl \\
        --staging data/eval_harness_staging/binary_catalyst.json \\
        [--multi-asset newest|skip|active] [--apply] [--limit N]

Default is dry-run. Pass `--apply` to actually INSERT.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

MultiAssetMode = str  # 'newest' | 'skip' | 'active'


@dataclass
class TickerMap:
    """Resolved (ticker → asset_id) under a chosen disambiguation mode.

    `ambiguous` records the multi-asset tickers that were either resolved
    via heuristic or skipped, so the run summary can surface them.
    """
    chosen: Dict[str, str] = field(default_factory=dict)        # ticker → asset_id
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)  # ticker → all asset_ids
    skipped: List[str] = field(default_factory=list)             # ticker (in --skip mode)


@dataclass
class EtlSummary:
    staged_total: int = 0
    staged_resolved: int = 0       # skip_category=None
    matched_tickers: int = 0
    matched_records: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    skipped_no_asset: int = 0
    skipped_multi_asset: int = 0
    by_hit: Counter = field(default_factory=Counter)
    errors: int = 0
    apply: bool = False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _load_staging(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"staging ledger not found: {path}")
    blob = json.loads(path.read_text())
    rows = blob.get("staging", [])
    if not isinstance(rows, list):
        raise ValueError(f"staging file shape unexpected: {path}")
    return rows


def _resolve_tickers(
    sb: SupabaseClient,
    tickers: Iterable[str],
    *,
    mode: MultiAssetMode,
) -> TickerMap:
    """Pull all `fda_assets` rows for the staged tickers, then pick one
    asset_id per ticker per the chosen disambiguation mode."""
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return TickerMap()

    # PostgREST `in.(...)` filter — comma-separated.
    in_filter = "in.(" + ",".join(tickers) + ")"
    rows = sb._rest("GET", "fda_assets", params={
        "select": "id,ticker,is_active,created_at",
        "ticker": in_filter,
        "limit": "10000",
    }) or []

    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    out = TickerMap()
    for tk, group in by_ticker.items():
        if len(group) == 1:
            out.chosen[tk] = group[0]["id"]
            continue

        out.ambiguous[tk] = [g["id"] for g in group]
        if mode == "skip":
            out.skipped.append(tk)
            continue

        if mode == "active":
            actives = [g for g in group if g.get("is_active") is True]
            pool = actives or group
        else:  # 'newest'
            pool = group

        # Newest by created_at among the chosen pool.
        winner = max(pool, key=lambda g: g.get("created_at") or "")
        out.chosen[tk] = winner["id"]

    return out


# ---------------------------------------------------------------------------
# Outcome derivation
# ---------------------------------------------------------------------------

def _derive_realized_outcome(label: Dict[str, Any]) -> str:
    """Compose a human-readable realized_outcome text from the label dict.

    Examples:
      hit=True               → 'binary_catalyst_hit_30d' (uses hit_window_days)
      hit=False              → 'binary_catalyst_miss' (the miss_reason has detail in jsonb)
      hit=None               → 'unresolved' (won't reach here in practice; staging filters)
    """
    hit = label.get("hit")
    if hit is True:
        win = label.get("hit_window_days")
        return f"binary_catalyst_hit_{win}d" if win else "binary_catalyst_hit"
    if hit is False:
        return "binary_catalyst_miss"
    return "unresolved"


def _build_eval_harness_row(
    *, asset_id: str, staged: Dict[str, Any],
) -> Dict[str, Any]:
    label = staged.get("label") or {}
    return {
        "asset_id": asset_id,
        "reference_assessment_date": staged["filed_at"],
        "realized_outcome": _derive_realized_outcome(label),
        "realized_outcome_data": label,
        "document_set": [],  # postgres uuid[] '{}'; backfill_document_set fills later
        "is_holdout": False,
        "difficulty": None,
        "notes": (
            f"phase4b_seed: from data/eval_harness_staging/binary_catalyst.json "
            f"event {staged.get('event_id') or '?'}"
        ),
        # D-105 columns:
        "tradeable_filter_pass": False,  # default; needs curation pipeline
        # issuer_status left null — TickerMap doesn't carry the column (active flag
        # is on fda_assets.is_active, not the historical issuer status at filed_at).
    }


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

def _existing_eval_keys(
    sb: SupabaseClient, asset_ids: Iterable[str],
) -> set[tuple[str, str]]:
    """Return the set of (asset_id, reference_assessment_date) pairs already
    in eval_harness for the given asset_ids."""
    asset_ids = sorted({a for a in asset_ids})
    if not asset_ids:
        return set()
    in_filter = "in.(" + ",".join(asset_ids) + ")"
    rows = sb._rest("GET", "eval_harness", params={
        "select": "asset_id,reference_assessment_date",
        "asset_id": in_filter,
        "limit": "10000",
    }) or []
    return {(r["asset_id"], r["reference_assessment_date"]) for r in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_etl(
    *,
    staging_path: Path,
    sb: Optional[SupabaseClient] = None,
    multi_asset: MultiAssetMode = "newest",
    limit: Optional[int] = None,
    apply: bool = False,
) -> EtlSummary:
    """Execute the subset ETL. Idempotent + dry-run-safe."""
    sb = sb or SupabaseClient()
    summary = EtlSummary(apply=apply)

    rows = _load_staging(staging_path)
    summary.staged_total = len(rows)

    resolved_rows = [r for r in rows if r.get("skip_category") is None]
    summary.staged_resolved = len(resolved_rows)

    if limit:
        resolved_rows = resolved_rows[:limit]

    # Step 1: ticker → asset_id resolution.
    tickers = {r["ticker"] for r in resolved_rows if r.get("ticker")}
    tmap = _resolve_tickers(sb, tickers, mode=multi_asset)
    summary.matched_tickers = len(tmap.chosen)

    if tmap.skipped:
        logger.info(
            "skipping multi-asset tickers (--multi-asset=skip): %s",
            sorted(tmap.skipped),
        )

    # Step 2: pre-load existing eval_harness keys for idempotency.
    existing = _existing_eval_keys(sb, tmap.chosen.values())

    # Step 3: walk the resolved rows, build INSERT payload list.
    payloads: List[Dict[str, Any]] = []
    for staged in resolved_rows:
        ticker = staged.get("ticker")
        if not ticker:
            summary.errors += 1
            continue

        asset_id = tmap.chosen.get(ticker)
        if not asset_id:
            if ticker in tmap.ambiguous and ticker in tmap.skipped:
                summary.skipped_multi_asset += 1
            else:
                summary.skipped_no_asset += 1
            continue

        key = (asset_id, staged["filed_at"])
        if key in existing:
            summary.skipped_existing += 1
            continue

        payloads.append(_build_eval_harness_row(asset_id=asset_id, staged=staged))
        existing.add(key)  # avoid intra-run duplicates if staging has them
        summary.matched_records += 1
        hit = (staged.get("label") or {}).get("hit")
        summary.by_hit["HIT" if hit is True else "MISS" if hit is False else "UNRESOLVED"] += 1

    if not apply:
        logger.info(
            "[dry-run] would INSERT %d rows (re-run with --apply to commit)",
            len(payloads),
        )
        summary.inserted = 0
        return summary

    # Step 4: batched INSERT. PostgREST handles arrays naturally.
    BATCH = 100
    for i in range(0, len(payloads), BATCH):
        batch = payloads[i : i + BATCH]
        try:
            sb._rest_with_retry(
                "POST", "eval_harness",
                json_body=batch,
                prefer="return=minimal",
            )
            summary.inserted += len(batch)
        except Exception as exc:  # noqa: BLE001
            logger.error("eval_harness INSERT batch %d failed: %s", i // BATCH, exc)
            summary.errors += len(batch)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--staging", type=Path,
        default=Path("data/eval_harness_staging/binary_catalyst.json"),
        help="staging ledger written by seed_eval_harness_from_export.py",
    )
    p.add_argument(
        "--multi-asset", default="newest",
        choices=["newest", "skip", "active"],
        help="how to disambiguate tickers with >1 fda_assets row",
    )
    p.add_argument("--limit", type=int, default=None, help="cap row count (debug)")
    p.add_argument(
        "--apply", action="store_true",
        help="actually INSERT (default is dry-run)",
    )
    args = p.parse_args(argv)

    summary = run_etl(
        staging_path=args.staging,
        multi_asset=args.multi_asset,
        limit=args.limit,
        apply=args.apply,
    )

    logger.info("ETL summary: %s", summary)
    # Exit non-zero if errors and not in dry-run.
    return 0 if summary.errors == 0 or not args.apply else 1


if __name__ == "__main__":
    sys.exit(main())
